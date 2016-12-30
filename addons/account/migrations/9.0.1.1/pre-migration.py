# -*- coding: utf-8 -*-
# © 2016 Sylvain LE GAL <https://twitter.com/legalsylvain>
# © 2016 Serpent Consulting Services Pvt. Ltd.
# © 2016 Eficent Business and IT Consulting Services S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
from openupgradelib import openupgrade

column_renames = {
    'account_bank_statement': [
        ('closing_date', 'date_done'),
    ],
    'account_account_type': [
        ('close_method', None),
    ],
    'account_bank_statement_line': [
        ('journal_entry_id', None),
    ],
    'account_account': [
        ('type', None),
    ],
    'account_cashbox_line': [
        ('pieces', 'coin_value'),
        ('number_opening', None),
        ('number_closing', None),
        ('bank_statement_id', None),
    ]
}

column_copies = {
    'account_bank_statement': [
        ('state', None, None),
    ],
    'account_journal': [
        ('type', None, None),
    ],
    'account_tax': [
        ('type_tax_use', None, None),
        ('type', None, None),
    ],
    'account_tax_template': [
        ('type_tax_use', None, None),
        ('type', None, None),
    ],
}

table_renames = [
    ('account_statement_operation_template', 'account_operation_template'),
    ('account_tax_code', 'account_tax_group')]

PROPERTY_FIELDS = {
    ('product.category', 'property_account_expense_categ',
     'property_account_expense_categ_id'),
    ('product.category', 'property_account_income_categ',
     'property_account_income_categ_id'),
    ('res.partner', 'property_account_payable', 'property_account_payable_id'),
    ('res.partner', 'property_account_receivable',
     'property_account_receivable_id'),
    ('res.partner', 'property_account_position',
     'property_account_position_id'),
    ('res.partner', 'property_payment_term', 'property_payment_term_id'),
    ('res.partner', 'property_supplier_payment_term',
     'property_supplier_payment_term_id'),
}


def migrate_properties(cr):
    for model, name_v8, name_v9 in PROPERTY_FIELDS:
        openupgrade.logged_query(cr, """
            update ir_model_fields
            set name = '{name_v9}'
            where name = '{name_v8}'
            and model = '{model}'
            """.format(model=model, name_v8=name_v8, name_v9=name_v9))
        openupgrade.logged_query(cr, """
            update ir_property
            set name = '{name_v9}'
            where name = '{name_v8}'
            """.format(name_v8=name_v8, name_v9=name_v9))


def no_remove_moves_exception_modules():
    """ In some countries the odoo standard closing procedure is not used,
    and the special periods should not be deleted."""
    return ['l10n_es_fiscal_year_closing']


def remove_account_moves_from_special_periods(cr):
    """We first search for journal entries in a special period, in the
    first reported fiscal year of the company, and we take them out of the
    special period, into a normal period, because we assume that this is
    the starting balance of the company, and should be maintained.
    Then we delete all the moves associated to special periods."""

    module_names = no_remove_moves_exception_modules()
    cr.execute("""
        SELECT * FROM ir_module_module
        WHERE name in %s
        AND state='installed'
    """, (tuple(module_names),))
    if cr.fetchall():
        return True

    cr.execute("""
        SELECT id FROM account_move
        WHERE period_id in (SELECT id FROM account_period WHERE special = True
        AND fiscalyear_id = (SELECT id FROM account_fiscalyear
        ORDER BY date_start ASC LIMIT 1) ORDER BY date_start ASC LIMIT 1)
    """)
    move_ids = [i for i, in cr.fetchall()]

    cr.execute("""
        SELECT id FROM account_period WHERE special = False
        AND fiscalyear_id = (SELECT id FROM account_fiscalyear
        ORDER BY date_start ASC LIMIT 1) ORDER BY date_start ASC LIMIT 1
    """)
    first_nsp_id = cr.fetchone()[0] or False

    if first_nsp_id and move_ids:
        openupgrade.logged_query(cr, """
            UPDATE account_move
            SET period_id = %s
            where id in %s
            """, (first_nsp_id, tuple(move_ids)))

    openupgrade.logged_query(cr, """
        DELETE FROM account_move_line
        WHERE move_id IN (SELECT id FROM account_move WHERE period_id IN (
        SELECT id FROM account_period WHERE special = True))
    """)

    openupgrade.logged_query(cr, """
        DELETE FROM account_move
        WHERE period_id IN (SELECT id FROM account_period WHERE special = True)
    """)


def install_account_tax_python(cr):
    """ Type tax type 'code' is in v9 introduced by module
    'account_tax_python. So, if we find an existing tax using this type,
    we know that we have to install the module."""
    openupgrade.logged_query(
        cr, "update ir_module_module set state='to install' "
        "where name='account_tax_python' "
        "and state in ('uninstalled', 'to remove') "
        "and exists (select id FROM account_tax where type = 'code')")


def map_account_tax_type(cr):
    """ The tax type 'code' is not an option in the account module for v9.
    We need to assign a temporary 'dummy' value until module
    account_tax_python is installed. In post-migration we will
    restore the original value."""
    openupgrade.map_values(
        cr,
        openupgrade.get_legacy_name('type'), 'type',
        [('code', 'group')],
        table='account_tax', write='sql')


def map_account_tax_template_type(cr):
    """Same comments as in map_account_tax_type"""
    openupgrade.map_values(
        cr,
        openupgrade.get_legacy_name('type'), 'type',
        [('code', 'group')],
        table='account_tax_template', write='sql')


def precreate_fields(cr):
    """Create computed fields that take long time to compute, but will be
    filled with valid values by migration."""

    def create_field(cr, table_name, field_name, pg_type, comment):
        cr.execute(
            'ALTER TABLE "%s" ADD COLUMN "%s" %s' %
            (table_name, field_name, pg_type)
        )
        cr.execute(
            'COMMENT ON COLUMN %s."%s" IS %%s' % (table_name, field_name),
            (comment,)
        )

    create_field(
        cr,
        "account_move", "currency_id", "int4",
        "Currency"
    )
    create_field(
        cr,
        "account_move", "amount", "numeric",
        "Amount"
    )
    create_field(
        cr,
        "account_move", "matched_percentage", "numeric",
        "Percentage Matched"
    )
    create_field(
        cr,
        "account_move_line", "amount_residual", "numeric",
        "Residual Amount"
    )
    create_field(
        cr,
        "account_move_line", "amount_residual_currency", "numeric",
        "Residual Amount in Currency"
    )
    create_field(
        cr,
        "account_move_line", "reconciled", "bool",
        "Reconciled"
    )
    create_field(
        cr,
        "account_move_line", "company_currency_id", "int4",
        "Utility field to express amount currency"
    )
    create_field(
        cr,
        "account_move_line", "balance", "numeric",
        "Technical field holding the debit - credit in order to open"
        " meaningful graph views from reports"
    )
    create_field(
        cr,
        "account_move_line", "debit_cash_basis", "numeric",
        "Debit Cash Basis"
    )
    create_field(
        cr,
        "account_move_line", "credit_cash_basis", "numeric",
        "Credit Cash Basis"
    )
    create_field(
        cr,
        "account_move_line", "balance_cash_basis", "numeric",
        "Balance Cash Basis"
    )
    # user_type_id has 'old' name user_type, but that is not in database:
    create_field(
        cr,
        "account_move_line", "user_type_id", "int4",
        "User Type Id"
    )
    # following field took hours to recompute.
    create_field(
        cr,
        "account_invoice_line", "price_subtotal_signed", "numeric",
        "Total amount in the currency of the company,"
        " negative for credit notes."
    )
    # Set fields on account_move
    # matched_percentage will be set to 0.0, but be filled in migration
    #     of reconciliations.
    openupgrade.logged_query(
        cr,
        """\
        UPDATE account_move
         SET currency_id = subquery.currency_id,
             amount = subquery.amount,
             matched_percentage = 0.0
         FROM (SELECT
                am.id as id,
                rc.currency_id as currency_id,
               sum(aml.debit) as amount
            FROM account_move am
            JOIN res_company rc ON rc.id = am.company_id
            LEFT OUTER JOIN account_move_line aml ON aml.move_id = am.id
            GROUP BY am.id, rc.currency_id
        ) as subquery
        WHERE account_move.id = subquery.id
        """
    )
    # cash basis fields depend om matched_percentage, which will be filled
    # by reconciliation migration. For now will be filled with values that
    # are correct if associated journal is not for sale or purchase.
    openupgrade.logged_query(
        cr,
        """\
        UPDATE account_move_line
        SET amount_residual = 0.0,
            amount_residual_currency = 0.0,
            reconciled = False,
            company_currency_id = subquery.company_currency_id,
            balance = subquery.balance,
            debit_cash_basis = subquery.debit,
            credit_cash_basis = subquery.credit,
            balance_cash_basis = subquery.balance,
            user_type_id = subquery.user_type_id
        FROM (
            SELECT
                aml.id as id,
                rc.currency_id as company_currency_id,
                (aml.debit - aml.credit) as balance,
                aml.debit as debit,
                aml.credit as credit,
                aa.user_type as user_type_id
            FROM account_move_line aml
            JOIN res_company rc ON rc.id = aml.company_id
            JOIN account_account aa ON aa.id = aml.account_id
        ) as subquery
        WHERE account_move_line.id = subquery.id
        """
    )
    # Set fields on account_invoice_line
    # TODO: rounding in SQL and currency-computation:
    openupgrade.logged_query(
        cr,
        """\
        WITH subquery(id, price_subtotal_signed) AS (
            SELECT
                ail.id,
                CASE
                    WHEN ai.type IN ('in_refund', 'out_refund')
                    THEN -ail.price_subtotal
                    ELSE ail.price_subtotal
                END
        FROM account_invoice_line ail
        JOIN account_invoice ai ON ail.invoice_id = ai.id
        )
        UPDATE account_invoice_line
        SET price_subtotal_signed = subquery.price_subtotal_signed
        FROM subquery
        WHERE account_invoice_line.id = subquery.id
        """
    )

@openupgrade.migrate()
def migrate(cr, version):
    # 9.0 introduces a constraint enforcing this
    cr.execute(
        "update account_account set reconcile=True "
        "where type in ('receivable', 'payable')"
    )
    openupgrade.rename_tables(cr, table_renames)
    openupgrade.rename_columns(cr, column_renames)
    openupgrade.copy_columns(cr, column_copies)
    migrate_properties(cr)
    install_account_tax_python(cr)
    map_account_tax_type(cr)
    map_account_tax_template_type(cr)
    remove_account_moves_from_special_periods(cr)
    precreate_fields(cr)
