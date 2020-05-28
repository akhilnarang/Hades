#!/usr/bin/env python3
"""
Flask application to accept some details, generate, display, and email a QR code to users
"""

# pylint: disable=invalid-name,too-few-public-methods,no-member,line-too-long,too-many-locals

import base64
import os
from datetime import datetime
from urllib.parse import urlparse, urljoin

from flask import Flask, redirect, render_template, request, url_for, jsonify, abort
from flask_cors import CORS
from flask_login import (
    LoginManager,
    login_required,
    login_user,
    logout_user,
    current_user,
    user_loaded_from_header,
)
from flask_login.utils import login_url
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect
from sqlalchemy.exc import DataError, IntegrityError

app = Flask(__name__)
cors = CORS(app)
app.secret_key = 'sadasdasdasdasd' #os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)

from .utils import (
    log,
    get_table_by_name,
    get_current_id,
    generate_qr,
    tg,
    get_accessible_tables,
    send_mail,
)

from . import api

# Import event related classes
import hades.models

# Import miscellaneous classes
from .models.user import Users, TSG
from .models.user_access import Access

# A list of currently active events
ACTIVE_TABLES = [models.giveaway.Coursera2020]
ACTIVE_EVENTS = ['Coursera 2020']

# The list of fields that will be required for any and all form submissions
REQUIRED_FIELDS = ('name', 'phone', 'email')


def is_safe_url(target: str) -> bool:
    """Returns whether or not the target URL is safe or a malicious redirect"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


@login_manager.user_loader
def load_user(user_id):
    """Return `User` object for the corresponding `user_id`"""
    return Users.query.get(user_id)


@login_manager.request_loader
def load_user_from_request(request):
    """Checks for authorization in a request

    The request can contain one of 2 headers
    -> Credentials: base64(username|password)
    or
    -> Authorization: api_token

    It first checks for the `Credentials` header, and then for `Authorization`
    If they match any user in the database, that user is logged into that session
    """
    credentials = request.headers.get('Credentials')
    if credentials:
        try:
            credentials = base64.b64decode(credentials).decode('utf-8')
        except UnicodeDecodeError:
            return None
        username, password = credentials.split('|')
        user = db.session.query(Users).get(username)
        if user is not None:
            if user.check_password_hash(password.strip()):
                log(
                    f'User <code>{user.name}</code> just authenticated a {request.method} API call with credentials!',
                )
                return user
    api_key = request.headers.get('Authorization')
    if api_key:
        # Cases where the header may be of the form `Authorization: Basic api_key`
        api_key = api_key.replace('Basic ', '', 1)
        users = db.session.query(Users).all()
        for user in users:
            if user.check_api_key(api_key):
                log(
                    f'User <code>{user.name}</code> just authenticated a {request.method} API call with an API key!',
                )
                return user
    return None


@login_manager.unauthorized_handler
def unauthorized():
    if 'Authorization' in request.headers or 'Credentials' in request.headers:
        return jsonify({'response': 'Access denied'}), 401

    # Generate the URL the login page should redirect to based on the URL user is trying to access in the same way
    # flask-login does so internally
    return redirect(login_url('login', request.url))


@app.route('/submit', methods=['POST'])
def submit():
    """Accepts form data for an event registration

    Some required fields are
    db -> The name of the database corresponding to the event. This will be hardcoded as an invisible uneditable field
    event -> The name of the event

    These fields can be skipped if ACTIVE_TABLES and ACTIVE_EVENTS lists have only one element
    This is done as we usually don't have more than 1 event at one time, we can reduce the risk of data being changed
    at the frontend by directly setting it here in the backend

    The rest of the parameters vary per event, we check the keys in the form items against the attributes of the
    corresponding event class to ensure that no extraneous data is accepted

    Some required ones are
    -> name - Person's name
    -> email - Person's email address
    -> department - Person's department

    Some optional fields which are *NOT* members of any class
    -> no_qr - To disable QR generation and attachment in email
    -> whatsapp_number - To store WhatsApp number separately
    -> chat_id - Alternative Telegram Chat ID where registrations should be logged
    -> email_content - Alternative email content to be sent to user
    -> email_formattable_content - content where some field needs to be replaced with the fields in email_content_fields
    -> email_content_fields - list of additional form fields which need to be replaced with variable's value in the email_formattable_content
    -> extra_message - Extra information to be appended to end of email
    -> extra_field_telegram - If anything besides ID and name are to be logged to Telegram, it is specified here

    The next three are specifically for events with group registrations
    -> name_second_person
    -> email_second_person
    -> department_second_person

    These are self explanatory

    Based on the data, a QR code is generated, displayed, and also emailed to the user(s).
    """

    # If there's just one active table, no need of checking
    if len(ACTIVE_TABLES) == 1:
        table = ACTIVE_TABLES[0]
    elif 'db' in request.form:
        table = get_table_by_name(request.form['db'])
        # Ensure that the provided table is active
        if table not in ACTIVE_TABLES:
            print(request.form)

            print(request.form['db'])
            log(
                f"Someone just tried to register to table <code>{request.form['db']}</code>"
            )
            form_data = ''
            for k, v in request.form.items():
                form_data += f'<code>{k}</code> - <code>{v}</code>\n'
            log(f'Full form:\n{form_data[:-1]}')
            return "That wasn't a valid db..."
    else:
        return "You need to specify a database!"

    # If we have only one active event - we know the event name already
    if len(ACTIVE_EVENTS) == 1:
        event_name = ACTIVE_EVENTS[0]
    elif 'event' in request.form:
        event_name = request.form['event']
    else:
        return 'Hades does require the event name, you know?'

    # Ensure that we have the required fields
    for field in REQUIRED_FIELDS:
        if field not in request.form:
            return f'<code>{field}</code> is required but has not been submitted!'

    # ID is from a helper function that increments the latest ID by 1 and returns it
    id = get_current_id(table)

    data = {}

    # Ensure that we only take in valid fields to create our user object
    for k, v in request.form.items():
        if k in table.__table__.columns._data.keys():
            data[k] = v

    # Instantiate our user object based on the received form data and retrived ID
    user = table(**data, id=id)

    # If a separate WhatsApp number has been provided, store that in the database as well
    if 'whatsapp_number' in request.form:
        try:
            if int(user.phone) != int(request.form['whatsapp_number']):
                user.phone += f"|{request.form['whatsapp_number']}"
        except (TypeError, ValueError) as e:
            form_data = ''
            for k, v in request.form.items():
                form_data += f'<code>{k}</code> - <code>{v}</code>\n'
            log(f'Exception on WhatsApp Number:\n{form_data[:-1]}')
            log(e)
            return "That wasn't a WhatsApp number..."

    # Store 2nd person's details ONLY if all 3 required parameters have been provided
    if (
        'name_second_person' in request.form
        and 'email_second_person' in request.form
        and 'department_second_person' in request.form
    ):
        user.name += f", {request.form['name_second_person']}"
        user.email += f", {request.form['email_second_person']}"
        user.department += f", {request.form['department_second_person']}"

    # Ensure that no data is duplicated. If anything is wrong, display the corresponding error to the user
    data = user.validate()
    if data is not True:
        return data

    # Generate the QRCode based on the given data and store base64 encoded version of it to email
    if 'no_qr' not in request.form:
        img = generate_qr(user)
        img.save('qr.png')
        img_data = open('qr.png', 'rb').read()
        encoded = base64.b64encode(img_data).decode()

    # Add the user to the database and commit the transaction, ensuring no integrity errors.
    try:
        db.session.add(user)
        db.session.commit()
    except (IntegrityError, DataError) as e:
        print(e)
        return """It appears there was an error while trying to enter your data into our database.<br/>Kindly contact someone from the team and we will have this resolved ASAP"""

    # Prepare the email sending
    from_email = os.getenv('FROM_EMAIL', 'noreply@thescriptgroup.in')
    to_emails = []
    email_1 = (request.form['email'], request.form['name'])
    to_emails.append(email_1)
    if (
        'email_second_person' in request.form
        and 'name_second_person' in request.form
        and request.form['email'] != request.form['email_second_person']
    ):
        email_2 = (
            request.form['email_second_person'],
            request.form['name_second_person'],
        )
        to_emails.append(email_2)

    # Check if the form specified the date, otherwise use the current month and year
    if 'date' in request.form:
        date = request.form['date']
    else:
        date = datetime.now().strftime('%B,%Y')
    subject = 'Registration for {} - {} - ID {}'.format(event_name, date, id)
    message = """<img src='https://drive.google.com/uc?id=12VCUzNvU53f_mR7Hbumrc6N66rCQO5r-&export=download' style='width:30%;height:50%'>
<hr>
{}, your registration is done!
<br/>
""".format(
        user.name
    )
    if 'no_qr' not in request.form:
        message += """A QR code has been attached below!
<br/>
You're <b>required</b> to present this on the day of the event."""
    if (
        'email_formattable_content' in request.form
        and 'email_content_fields' in request.form
    ):
        d = {}
        for f in request.form['email_content_fields'].split(','):
            d[f] = request.form[f]
        message = ''
        if 'email_content' in request.form:
            message += request.form['email_content']
        message += request.form['email_formattable_content'].format(**d)

    if 'extra_message' in request.form:
        message += '<br/>' + request.form['extra_message']

    # Take care of attachments, if any
    attachments = []
    if 'no_qr' not in request.form:
        attachments.append(
            {'data': encoded, 'filename': 'qr.png', 'type': 'image/png',}
        )

    # Send the mail
    mail_sent = send_mail(from_email, to_emails, subject, message, attachments)

    # Log the new entry to desired telegram channel
    chat_id = (
        request.form['chat_id'] if 'chat_id' in request.form else os.getenv('GROUP_ID')
    )
    caption = f'Name: {user.name} | ID: {user.id}'
    if 'extra_field_telegram' in request.form:
        caption += f" | {request.form['extra_field_telegram']} - {request.form[request.form['extra_field_telegram']]}"

    tg.send_chat_action(chat_id, 'typing')
    tg.send_message(chat_id, f'New registration for {event_name}!')
    if 'no_qr' not in request.form:
        tg.send_document(chat_id, caption, 'qr.png')
    else:
        tg.send_message(chat_id, caption)

    ret = f'Thank you for registering, {user.name}!'
    if 'no_qr' not in request.form:
        ret += "<br>Please save this QR Code. "
        if mail_sent:
            ret += "It has also been emailed to you."
        ret += "<br><img src=\
                'data:image/png;base64, {}'/>".format(
            encoded
        )
    else:
        ret += '<br>Please check your email for confirmation.'
    return ret


@app.route('/login', methods=['GET', 'POST'])
def login():
    """
    Displays a login page on a GET request
    For POST, it checks the `username` and `password` provided and accordingly redirects to desired page
    (events page if none specified)

    It *will* abort with a 400 error if the `next` parameter is trying to redirect to an unsafe URL
    """
    if request.method == 'POST':
        user = request.form['username']
        user = db.session.query(Users).filter_by(username=user).first()
        # Ensure user exists in the database
        if user is not None:
            password = request.form['password']
            # Check the password against the hash stored in the database
            if user.check_password_hash(password):
                # Log the login and redirect
                log(f'User <code>{user.name}</code> logged in via webpage!')
                login_user(user)
                next = request.args.get('next')
                if not is_safe_url(next):
                    return abort(400)
                return redirect(next or url_for('events'))
            return f'Wrong password for {user.username}!'
        return f"{request.form['username']} doesn't exist!"
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """
    Displays a registration page on a GET request
    For POST, it checks the `name`, `email`, `username` and `password` provided and accordingly registers account

    A random 32 character API key is generated and displayed

    Password and API key are hashed before being stored in the database
    """
    if request.method == 'POST':
        required_fields = ('name', 'username', 'password', 'email')
        for field in required_fields:
            if field not in request.form:
                return f'{field} is required!'
        name = request.form['name']
        username = request.form['username']
        password = request.form['password']
        email = request.form['email']

        # Create user object
        u = Users()
        u.name = name
        u.username = username
        u.generate_password_hash(password)
        u.email = email
        api_key = u.generate_api_key()

        # Add the user object to the database
        db.session.add(u)

        # If you're a TSG member, you get some access by default
        if TSG.query.filter(TSG.email == email).first():
            db.session.add(Access('tsg', username))
            db.session.add(Access('test_users', username))

        # Commit the transaction and confirm that no integrity constraints have been violated
        try:
            db.session.commit()
        except IntegrityError:
            return (
                jsonify(
                    {
                        'response': 'Integrity constraint violated, please re-check your data!'
                    }
                ),
                400,
            )
        log(f'User <code>{u.name}</code> account has been registered!')
        return f"Hello {username}, your account has been successfully created.<br>If you wish to use an API Key for sending requests, your key is <code>{api_key}</code><br/>Don't share it with anyone, if you're unsure of what it is, you don't need it"
    return render_template('register.html')


@app.route('/events', methods=['GET', 'POST'])
@login_required
def events():
    """
    Displays a page with a dropdown to choose events on a GET request
    For POST, it checks the `table` provided and accordingly returns a table listing the users in that table
    """
    if request.method == 'POST':
        if 'table' not in request.form:
            return jsonify(
                {'response': 'Please specify the table you want to access!'}, 400
            )
        table_name = request.form['table']
        table = get_table_by_name(table_name)
        if table is None:
            return jsonify({'response': f'Table {table} does not seem to exist!'}, 400)
        log(
            f"User <code>{current_user.name}</code> is accessing <code>{request.form['table']}</code>!"
        )
        user_data = db.session.query(table).all()
        return render_template(
            'users.html', users=user_data, columns=table.__table__.columns._data.keys()
        )
    return render_template('events.html', events=get_accessible_tables())


@app.route('/update', methods=['GET', 'POST'])
@login_required
def update():
    """
    Displays a page with a dropdown to choose events on a GET request
    For POST, there for 2 cases
    If a `field` is not provided, it gets table table from the form and returns a page where user can choose a field
    If a field is provided, that field of the corresponding user is updated (table and a key attribute are taken from
    the form as well)
    """
    if request.method == 'POST':
        if 'field' not in request.form:
            table_name = request.form['table']
            table = get_table_by_name(table_name)
            i = inspect(table)
            fields = i.columns.keys()
            for f in fields:
                if i.columns[f].primary_key or i.columns[f].unique:
                    fields.remove(f)

            return render_template('update.html', fields=fields, table_name=table_name,)

        table = get_table_by_name(request.form['table'])
        if table is None:
            return 'Table not chosen?'

        user = db.session.query(table).get(request.form[request.form['key']])
        setattr(user, request.form['field'], request.form['value'])

        try:
            db.session.commit()
        except IntegrityError:
            return 'Integrity constraint violated, please re-check your data!'

        log(
            f"<code>{current_user.name}</code> has updated <code>{request.form['field']}</code> of <code>{user}</code> to <code>{request.form['value']}</code>"
        )
        return 'User has been updated!'
    return render_template('events.html', events=get_accessible_tables())


@app.route('/changepassword', methods=['GET', 'POST'])
@login_required
def change_password():
    """
       Displays a page to enter current and a new password on a GET request
       For POST, changes the password if current one matches and logs you out
    """

    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']

        # If current password is correct, update and store the new hash
        if current_user.check_password_hash(current_password):
            current_user.generate_password_hash(new_password)
        else:
            return 'Current password you entered is wrong! Please try again!'

        # Complete the transaction. No exceptions should occur here
        db.session.commit()

        log(f'<code>{current_user.name}</code> has updated their password!</code>')

        # Log the user out, and redirect to login page
        logout_user()
        return redirect(url_for('login'))
    return render_template('change_password.html')


@app.route('/logout')
@login_required
def logout():
    """Logs the current user out"""
    name = current_user.name
    logout_user()
    return f"Logged out of {name}'s account!"


@app.route('/')
def root():
    """Root endpoint. Displays the form to the user."""
    return '<marquee>Nothing here!</marquee>'
