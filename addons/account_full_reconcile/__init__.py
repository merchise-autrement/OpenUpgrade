# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
from openerp import SUPERUSER_ID
from openerp.tools.float_utils import float_round, float_is_zero

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
            self.combined_reconcile_id = db_record[1]
            self.reconcile_id = db_record[2]
            self.reconcile_ref = db_record[3]
            self.debit = db_record[4]
            self.credit = db_record[5]
            self.date = db_record[6]
            self.amount_residual = db_record[7]  # Start off with full amount
            self.line_currency_id = db_record[8]
            self.line_currency_rounding = db_record[9]
            self.company_currency_id = db_record[10]
            self.company_currency_rounding = db_record[11]
            self.amount_residual_currency = 0.0  # Not in 8.0

    def reconcile_records(cr, debit_record, credit_record, full_reconcile_id):
        """Links a credit and debit line through partial reconciliation."""
        amount = min(debit_record.debit, credit_record.credit)
        amount_currency = 0
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
            VALUES(%s, %s, %s, %s, %s,
                   CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP, %s)
            """,
            params=(
                amount,
                amount_currency,
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

    def update_account_move_line(cr, move_lines, full_reconcile_id):
        """Update move lines."""
        # TODO: compute amount_currency
        for line in move_lines:
            # Compute reconciled similar to what happens in model, but using
            # data retrieved using SQL:
            reconciled = False
            digits_rounding_precision = line.company_currency_rounding
            if float_is_zero(
                    line.amount_residual,
                    precision_rounding=digits_rounding_precision):
                if line.line_currency_id and line.amount_residual_currency:
                    # if there is an amount in another currency, it must
                    # be zero as well:
                    currency_zero = float_is_zero(
                        line.amount_residual_currency,
                        precision_rounding=line.line_currency_id_rounding
                    )
                    if currency_zero:
                        reconciled = True
                else:
                    # no currency involved:
                    reconciled = True
            cr.execute(
                """
                UPDATE account_move_line SET
                    amount_residual = %s,
                    amount_residual_currency = %s,
                    reconciled = %s,
                    write_date = CURRENT_TIMESTAMP,
                    write_uid = %s
                WHERE id = %s
                """,
                params=(
                    float_round(
                        line.amount_residual,
                        precision_rounding=line.company_currency_rounding
                    ),
                    float_round(
                        line.amount_residual_currency,
                        precision_rounding=line.company_currency_rounding
                    ),
                    reconciled,
                    SUPERUSER_ID,
                    line.id
                )
            )

    def handle_complete_reconciliation(cr, debit_lines, credit_lines):
        """Each time a move line has another reconcile id, we can
        migrate the 8.0 reconciliation in full."""
        if not debit_lines or not credit_lines:
            return
        record = debit_lines[0]
        full_reconcile_id = (
            record.reconcile_id and
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
                    credit_record.amount_residual < 0):
                reconcile_records(
                    cr, debit_record, credit_record, full_reconcile_id
                )
            if debit_record.amount_residual <= 0:
                current_debit += 1
            if credit_record.amount_residual >= 0:
                current_credit += 1
        # Update amount residual in reconciled records:
        update_account_move_line(cr, debit_lines, full_reconcile_id)
        update_account_move_line(cr, credit_lines, full_reconcile_id)

    # Define generator to loop over potentially huge amount of records in
    # cursor
    def result_iter(cursor, arraysize=1024):
        'An iterator that uses fetchmany to keep memory usage down'
        while True:
            try:
                results = cursor.fetchmany(arraysize)
            except:
                # Assume no more rows to fetch
                break
            if not results:
                break
            for result in results:
                yield result

    # We need information on possibly two currencies. One for the company
    # one for the alternative currency defined in the line. For clarity
    # we will prefix the first with company_currency_ and the other with
    # line_currency_
    cr.execute(
        """
        SELECT
            aml.id AS id,
            COALESCE(aml.reconcile_id, aml.reconcile_partial_id)
                AS combined_reconcile_id,
            COALESCE(aml.reconcile_id, 0)
                AS reconcile_id,
            aml.reconcile_ref,
            aml.debit,
            aml.credit,
            aml.date AS date,
            aml.debit - aml.credit AS amount_residual,
            aml.currency_id as line_currency_id,
            line_cur.rounding AS line_currency_rounding,
            com.currency_id as company_currency_id,
            company_cur.rounding AS company_currency_rounding
        FROM account_move_line aml
        JOIN res_company com on aml.company_id = com.id
        LEFT OUTER JOIN res_currency line_cur
            ON aml.currency_id = line_cur.id
        LEFT OUTER JOIN res_currency company_cur
            ON com.currency_id = company_cur.id
        WHERE aml.reconcile_id IS NOT NULL
           OR aml.reconcile_partial_id IS NOT NULL
        ORDER BY
            aml.reconcile_id, aml.reconcile_partial_id, aml.date
        """
    )
    current_id = False
    debit_lines = []
    credit_lines = []
    for db_record in result_iter(cr):
        record = LineRecord(db_record)
        if current_id and current_id != record.combined_reconcile_id:
            handle_complete_reconciliation(cr, debit_lines, credit_lines)
            debit_lines = []  # Reset
            credit_lines = []  # Reset
        current_id = record.combined_reconcile_id
        if record.credit:
            credit_lines.append(record)
        else:
            debit_lines.append(record)
    # Do remaining bunch of records:
    if current_id:
        handle_complete_reconciliation(cr, debit_lines, credit_lines)
