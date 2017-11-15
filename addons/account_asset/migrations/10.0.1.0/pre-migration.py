# -*- coding: utf-8 -*-
# Copyright 2017 Tecnativa - Vicent Cubells
# Copyright 2017 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from openupgradelib import openupgrade


column_renames = {
    'account_move': [
        ('asset_id', None),
    ],
}

field_renames = [
    # renamings with oldname attribute - They also need the rest of operations
    ('account.asset.category', 'account_asset_category',
     'account_income_recognition_id', 'account_depreciation_expense_id'),
]


@openupgrade.migrate(use_env=True)
def migrate(env, version):
    openupgrade.rename_columns(env.cr, column_renames)
    openupgrade.rename_fields(env, field_renames)
