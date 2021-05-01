import os
from .. import db
from .. import app
import flask_login
from flask_login import login_required
from flask import render_template
import flask
from flask import jsonify
from flask import request
from bson import ObjectId

@app.route("/data/user", methods=["GET"])
def data_user_get():
    if request.args.get('id') == 'new':
        return jsonify([{}])
    filter = {}
    if request.args.get('id'):
        filter = {'_id': ObjectId(request.args.get('id'))}
    user = db.users.find_one(filter)
    return jsonify([user])

@app.route("/data/user", methods=["POST"])
def data_user_post():
    filter = {}
    f = request.form
    from .auth import ADMIN_USER
    if f['login'].lower() == ADMIN_USER.lower():
        raise Exception("invalid user name")
    f = dict(f)
    if '_id' in f:
        filter = {'_id': ObjectId(f.pop('_id'))}
    else:
        filter = {'login': f['login']}
    db.users.update_one(filter, {"$set": f}, upsert=True)

    return jsonify(request.form)

@app.route("/data/users", methods=["GET", "POST"])
def data_users():
    filter = {}
    if request.args.get('id'):
        filter = {'_id': ObjectId(request.args.get('id'))}
    users = list(db.users.find(filter))
    for user in users:
        user['all_sites'] = list(db.sites.find({}, {'name': 1}))
    return jsonify(users)

@app.route("/data/user/delete", methods=["POST"])
def data_users_delete():
    filter = {'_id': ObjectId(request.form.get('id'))}
    db.users.remove(filter)
    return jsonify({'result': 'ok'})

@app.route("/data/user_sites", methods=["GET", "POST"])
def data_user_sites():
    _filter = {'_id': ObjectId(request.args.get('user_id', request.form.get('user_id')))}
    user = db.users.find_one(_filter, {'name': 1, 'sites': 1}) or {}
    user.setdefault('sites', [])

    if request.method == "GET":
        sites = []
        for site in db.sites.find({}, {'name': 1}):
            sites.append({
                'name': site['name'],
                'allowed': site['name'] in user['sites'],
            })
        return jsonify(sites)
    else:
        name = request.form['name']
        if request.form['allowed'] == '1':
            user['sites'].append(name)
        else:
            if name in user['sites']:
                user['sites'].remove(name)
        user['sites'] = list(set(user['sites']))

        db.users.update_one(_filter, {"$set": user})
        return jsonify({"result": "ok"})

@app.route('/user_admin')
@login_required
def users_index():

    if flask_login.current_user.is_authenticated and flask_login.current_user.is_admin:
        return render_template(
            'user_admin.html',
            DATE_FORMAT=os.environ['DATE_FORMAT'].replace("_", "%"),
        )
    raise Exception("unauthorized")