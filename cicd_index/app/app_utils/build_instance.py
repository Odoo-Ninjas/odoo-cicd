
@app.route("/last_successful_sha")
def last_success_full_sha():
    info = {
        'name': request.args['name'],
    }
    assert info['name']

    updates = list(db.updates.find(info).sort([("date", pymongo.DESCENDING)]).limit(1))
    if updates:
        return jsonify({
            'sha': updates[0]['sha']
        })
    return jsonify({
        'sha': '',
    })


def _reset_instance_in_db(name):
    info = {
        'name': request.args['name'],
    }
    db.sites.remove(info)
    db.updates.remove(info)

def _set_marker_and_restart(name, settings):
    site = db.sites.find_one({'name': name})
    db.sites.update_one(
        {'_id': site['_id']},
        {'$set': settings},
        upsert=False
    )

    jenkins = _get_jenkins()
    job = jenkins[f"{os.environ['JENKINS_JOB_MULTIBRANCH']}/{site['git_branch']}"]
    job.invoke()
    return jsonify({
        'result': 'ok',
    })
