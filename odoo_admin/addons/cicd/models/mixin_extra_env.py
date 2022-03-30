from odoo import _, api, fields, models, SUPERUSER_ID, tools
from contextlib import contextmanager, closing


class MixinExtraEnv(models.AbstractModel):
    _name = 'cicd.mixin.extra_env'

    @contextmanager
    def _extra_env(self, obj=None, enabled=True):
        obj = obj or self
        obj.ensure_one()
        if not enabled:
            yield obj
        else:

            # avoid long locking
            with closing(self.env.registry.cursor()) as cr:
                env = api.Environment(cr, SUPERUSER_ID, self._context)
                env.reset()
                obj = obj.with_env(env).with_context(prefetch_fields=False)

                try:
                    yield obj

                finally:
                    env.cr.rollback()
                    env.clear()

    def _unblocked_read(self, fields):
        with self._extra_env() as self:
            res = {}
            for field in fields:
                res[field] = self[field]
        return res

    def _unblocked(self, field):
        with self._extra_env() as self:
            res = self[field]
        return res
