from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import arrow

class Schedule(models.AbstractModel):
    _name = 'mixin.schedule'

    # TODO: add days, and so on

    hour = fields.Integer("Hour")
    minute = fields.Integer("Minute")
    
    def _compute_next_date_greater_now(self, start_from):
        d = self._compute_next_date(
            start_from
        )
        now = fields.Datetime.now()
        if d < fields.Datetime.to_string(now):
            d = self._compute_next_date(now)
        return d

    @api.model
    def _compute_next_date(self, start_from):
        test = arrow.get(
            (start_from and arrow.get(start_from) or arrow.utcnow()).strftime(
                DTF))
        test = test.replace(hour=self.hour, minute=self.minute)
        if test.strftime(DTF) < (start_from or arrow.utcnow()).strftime(DTF):
            test = test.shift(days=1)
        return test.strftime(DTF)
