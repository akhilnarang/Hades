#!/usr/bin/env python3

from flask_sqlalchemy import SQLAlchemy

# Import event related classes
from hades.models.csi import CSINovember2019, CSINovemberNonMember2019
from hades.models.codex import CodexApril2019, RSC2019
from hades.models.techo import EHJuly2019, P5November2019
from hades.models.workshop import (
    CPPWSMay2019,
    CCPPWSAugust2019,
    Hacktoberfest2019,
    CNovember2019,
    BitgritDecember2019,
)

# Import miscellaneous classes
from hades.models.test import TestTable
from hades.models.user import Users
from hades.models.event import Events
from hades.models.user_access import Access

from flask import Flask

from hades import app, db, EVENT_CLASSES

# Source is taken from app
src = input('Enter source DB URI: ')
dest = input('Enter destination DB URI: ')

dest_app = Flask(__name__)
dest_db = SQLAlchemy(dest_app)
dest_app.config['SQLALCHEMY_DATABASE_URI'] = dest


app.config['SQLALCHEMY_DATABASE_URI'] = src
print('Source tables: ')
print(db.engine.table_names())
print('Running create_all() on destination URI')
app.config['SQLALCHEMY_DATABASE_URI'] = dest
db.create_all()
print(db.engine.table_names())
app.config['SQLALCHEMY_DATABASE_URI'] = src


tables = db.engine.table_names()
tables.reverse()
for table in tables:
    print(f'Checking entries in {table}')
    table = EVENT_CLASSES[table]
    if table is None:
        print('Table is None')
        continue
    data = db.session.query(table).all()
    for i in data:
        user_data = table()
        for k, v in i.__dict__.items():
            if k == '_sa_instance_state':
                continue
            setattr(user_data, k, v)
        dest_db.session.add(user_data)
    dest_db.session.commit()
