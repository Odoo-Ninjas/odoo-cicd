import flask
import os
from flask.templating import render_template
from flask import redirect
import flask_login
from flask import request
from .. import app
from .. import db
from .. import login_manager
from .models import User

ADMIN_USER = 'admin'

class User(flask_login.UserMixin):
    id = ""
    is_authenticated = False


@login_manager.user_loader
def user_loader(login):
    # if email not in users:
    #     return

    user = User()

    if login != ADMIN_USER:
        userdb = db.users.find_one({'login': login})
        if not userdb:
            raise Exception("Unauthorized")

        user.is_admin = False
    else:
        user.is_admin = True
    user.id = login
    user.is_authenticated = True
    return user

@app.route('/logout')
def logout():
    flask_login.logout_user()
    return redirect("/cicd/login")

@app.route('/login', methods=['GET'])
def login():
    if request.method == 'GET':
        return render_template(
            'login.html',
        )

@app.route('/login', methods=['POST'])
def login_post():
    login = flask.request.form['username']
    authorized = False
    password = flask.request.form['password']
    
    if login == ADMIN_USER:
        authorized = password == os.getenv("PASSWD")
    else:
        user = db.users.find_one({'login': login}, {'password': 1})
        if user:
            authorized = password == user.get('password')
    
    if authorized:
        user = User()
        user.id = login
        flask_login.login_user(user)
        return flask.redirect('/index')
    return flask.redirect("/cicd/login?error=1")
    
@login_manager.unauthorized_handler
def unauthorized_handler():
    if os.getenv("PASSWD"):
        return login()
    else:
        user = User()
        user.id = 'admin'
        user.authenticated = True
        user.is_admin = True
        flask_login.login_user(user)
        return redirect("/cicd/index")

    
@app.route("/user/is_admin")
def is_admin():
    user = flask_login.current_user
    if user.is_authenticated:
        return jsonify({
            'admin': user.is_admin,
        })
    return jsonify({'admin': False})