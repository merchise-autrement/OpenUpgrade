# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
# © 2016 Therp BV.
from openerp import SUPERUSER_ID
from openerp.tools.float_utils import float_round

import models


def _migrate_full_reconcile(cr, registry):
    """Migrate account.move.reconcile to account.partial.reconcile and
    account.full.reconcile.

    TODO: Look into amount_residual_currency and foreign exchange writeoff.
    """
    # avoid doing anything if the table has already something in it
    # (already migrated)
    cr.execute("""SELECT count(id) FROM account_full_reconcile""")
    res = cr.fetchone()[0]
    if res:
        return

    class LineRecord:

        def __init__(self, db_record):
            """Initialize attributes from db record."""
            self.id = db_record[0]
            self.reconcile_id = db_record[1]
            self.full_reconcile = db_record[2]
            self.reconcile_ref = db_record[3]
            self.debit = db_record[4]
            self.credit = db_record[5]
            self.currency_id = db_record[6]
            self.date = db_record[7]
            self.rounding = db_record[8]
            self.amount_residual = 0.0  # Not in 8.0
            self.amount_residual_currency = 0.0  # Not in 8.0

    def reconcile_records(cr, debit_record, credit_record, full_reconcile_id):
        """Links a credit and debit line through partial reconciliation."""
        amount = min(debit_record.debit, credit_record.credit)
        cr.execute(
            """
            INSERT INTO account_partial_reconcile (
                amount,
                amount_currency,
                credit_move_id,
                debit_move_id,
                full_reconcile_id,
                create_date,
                create_uid,
                write_date,
                write_uid
            )
            VALUES(%s, %s, %s, %s,
                   CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP, %s)
            """,
            params=(
                amount,
                credit_record.id,
                debit_record.id,
                full_reconcile_id or None,
                SUPERUSER_ID,
                SUPERUSER_ID
            )
        )
        debit_record.amount_residual -= amount
        credit_record.amount_residual += amount
        debit_record.amount_residual_currency -= amount_currency
        credit_record.amount_residual_currency += amount_currency

    def create_full_reconcile(cr, name):
        """Creates full reconcile and returns id of new record."""
        cr.execute(
            """
            INSERT INTO account_full_reconcile (
                name,
                create_date,
                create_uid,
                write_date,
                write_uid
            )
            VALUES(%s, CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP, %s)
            RETURNING id
            """,
            params=(
                name,
                SUPERUSER_ID,
                SUPERUSER_ID,
            )
        )
        return cr.fetchone()[0]

    def update_amount_residual(cr, move_lines):
        """Update amount residual in move lines."""
        # TODO: compute amount_currency
        for line in move_lines:
            cr.execute(
                """
                UPDATE account_move_line SET
                    amount_residual = %s,
                    amount_residual_currency = %s,
                    write_date = CURRENT_TIMESTAMP,
                    write_uid = %s
                WHERE id = %s
                """,
                params=(
                    float_round(
                        line.amount_residual,
                        line.amount_residual_currency,
                        precision_rounding=line.rounding
                    ),
                    SUPERUSER_ID,
                    line.id
                )
            )

    def handle_complete_reconciliation(cr, debit_lines, credit_lines):
        """Each time a move line has another reconcile id, we can
        migrate the 8.0 reconciliation in full."""
        record = debit_lines[0]
        full_reconcile_id = (
            record.full_reconcile and
            create_full_reconcile(cr, record.reconcile_ref) or
            False
        )
        # 1. Reconcile equal amounts:
        for debit_record in debit_lines:
            for credit_record in credit_lines:
                if debit_record.debit == credit_record.credit:
                    reconcile_records(
                        cr, debit_record, credit_record, full_reconcile_id
                    )
                    break
        # 4. Reconcile unequal amounts:
        current_debit = 0
        current_credit = 0
        last_debit = len(debit_lines) - 1
        last_credit = len(credit_lines) - 1
        while current_debit <= last_debit and current_credit <= last_credit:
            debit_record = debit_lines[current_debit]
            credit_record = credit_lines[current_credit]
            if (debit_record.amount_residual > 0 and
                    credit_record.amount_residual > 0):
                reconcile_records(
                    cr, debit_record, credit_record, full_reconcile_id
                )
            if debit_record.amount_residual <= 0:
                current_debit += 1
            if credit_record.amount_residual <= 0:
                current_credit += 1
        # Update amount residual in reconciled records:
        update_amount_residual(cr, debit_lines)
        update_amount_residual(cr, credit_lines)

    cr.execute(
        """
        SELECT
            aml.id AS id,
            COALESCE(aml.reconcile_id, aml.reconcile_partial_id)
                AS reconcile_id,
            COALESCE(aml.reconcile_id, 0)
                AS full_reconcile,
            aml.reconcile_ref,
            aml.debit,
            aml.credit,
            aml.currency_id,
            aml.date AS date,
            cur.rounding AS rounding
            aml.debit - aml.credit AS amount_residual,
        FROM account_move_line aml
        JOIN res_company com on aml.company_id = com.id
        JOIN res_currency cur on com.currency_id = cur.id
        WHERE aml.reconcile_id IS NOT NULL
           OR aml.reconcile_partial_id IS NOT NULL
        ORDER BY
            aml.reconcile_id, aml.reconcile_partial_id, aml.date
        """
    )
    current_id = False
    debit_lines = []
    credit_lines = []
    for db_record in cr.fetchall():
        record = LineRecord(db_record)
        if current_id and current_id != record.reconcile_id:
            handle_complete_reconciliation(cr, debit_lines, credit_lines)
            debit_lines = []  # Reset
            credit_lines = []  # Reset
        current_id = record.reconcile_id
        if record.credit:
            record.amount_residual = record.credit
            credit_lines.append(record)
        else:
            record.amount_residual = record.debit
            debit_lines.append(record)
    # Do remaining bunch of records:
    if current_id:
        handle_complete_reconciliation(cr, debit_lines, credit_lines)
