from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    cr.execute((
        "delete from cicd_release_item "
        "where state not in ('failed', 'done')"
    ))
    cr.execute((
        "update cicd_release_item "
        "set state = 'failed_technically' "
        "where state = 'failed'"
    ))
    cr.execute((
        "delete from ir_cron "
        "where name ilike '%scheduled release%'"
    ))