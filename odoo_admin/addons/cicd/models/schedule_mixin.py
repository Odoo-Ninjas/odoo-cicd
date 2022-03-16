from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import arrow

class Schedule(models.AbstractModel):
    _name = 'mixin.schedule'

    # TODO: add days, and so on

    hour = fields.Integer("Hour")
    minute = fields.Integer("Minute")

    def get_next(self, start_from=None):
        start_from = arrow.get(start_from or arrow.get())
        
        test = start_from
        test = test.replace(hour=self.hour, minute=self.minute)
        if test < start_from:
            test = test.shift(days=1)
        return test
