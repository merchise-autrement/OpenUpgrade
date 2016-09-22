# -*- coding: utf-8 -*-
# © 2016 Therp BV.
from openerp import SUPERUSER_ID
from openupgradelib import openupgrade


def account_partial_reconcile(env):
    """Migrate account.move.reconcile to account.partial.reconcile and
    account.full.reconcile.

    TODO: Look into amount_residual_currency and foreign exchange writeoff.
    """
    def reconcile_records(cr, debit_record, credit_record, full_reconcile_id):
        """Links a credit and debit line through partial reconciliation."""
        amount = min(debit_record.debit, credit_record.credit)
        cr.execute(
            """
            INSERT INTO account_partial_reconcile (
                amount,
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
        credit_record.amount_residual -= amount

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

    def update_amount_remaining(cr, move_lines):
        """Update amount remaining in move lines."""
        # TODO: compute amount_currency
        for line in move_lines:
            cr.execute(
                """
                UPDATE account_move_line SET
                    amount_remaining = %s,
                    write_date = CURRENT_TIMESTAMP,
                    write_uid = %s
                WHERE id = %s
                """,
                params=(
                    line.amount_remaining,
                    SUPERUSER_ID,
                    line.id
                )
            )

    cr = env.cr
    cr.execute(
        """
        SELECT
            id,
            COALESCE(reconcile_id, reconcile_partial_id) as reconcile_id,
            COALESCE(reconcile_id, 0) as full_reconcile,
            reconcile_ref,
            debit,
            credit,
            date
        FROM account_move_line
        WHERE
            reconcile_id IS NOT NULL or reconcile_partial_id IS NOT NULL
        ORDER BY
            reconcile_id, reconcile_partial_id, date
        """
    )

    class LineRecord:

        def __init__(self, db_record):
            """Initialize attributes from db record."""
            self.id = db_record[0]
            self.reconcile_id = db_record[1]
            self.full_reconcile = db_record[2]
            self.reconcile_ref = db_record[3]
            self.debit = db_record[4]
            self.credit = db_record[5]
            self.date = db_record[6]
            self.amount_residual = 0.0

    for db_record in cr.fetchall():
        record = LineRecord(db_record)
        current_id = record.reconcile_id
        full_reconcile_id = (
            record.full_reconcile and
            create_full_reconcile(cr, record.reconcile_ref) or
            False
        )
        debit_lines = []
        credit_lines = []
        while record.reconcile_id == current_id:
            if record.credit:
                record.amount_residual = record.credit
                credit_lines.append(record)
            else:
                record.amount_residual = record.debit
                debit_lines.append(record)
        # getting here we have an array of lines that we will reconcile.
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
        # Update amount remaining in reconciled records:
        update_amount_remaining(cr, debit_lines)
        update_amount_remaining(cr, credit_lines)


@openupgrade.migrate(use_env=True)
def migrate(env, version):
    account_partial_reconcile(env)
