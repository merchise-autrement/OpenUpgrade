# -*- coding: utf-8 -*-
# Copyright 2017 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from openupgradelib import openupgrade

COLUMN_RENAMES = {
    'account_analytic_account': [
        ('use_timesheets', None),
    ]
}


def create_and_populate_department(cr):
    cr.execute('''
       ALTER TABLE account_analytic_line ADD COLUMN department_id INT;
       ALTER TABLE account_analytic_line
          ADD CONSTRAINT account_analytic_line_department_id_fkey
          FOREIGN KEY (department_id) REFERENCES hr_department (id);

       WITH departments AS (
          SELECT r.user_id AS user_id, MAX(e.department_id) AS dpt_id
             FROM hr_employee e JOIN resource_resource r
                                ON e.resource_id = r.id
          GROUP BY user_id
       )
       UPDATE account_analytic_line aal SET department_id=departments.dpt_id
       FROM departments WHERE aal.user_id = departments.user_id;
    ''')


def create_and_populate_children(cr):
    openupgrade.logged_query(cr, '''
    ALTER TABLE project_task ADD COLUMN parent_id INT;

    -- Since the parent_id is new there's no point in computing the
    -- children_hours, they will be 0; however there's a single method
    -- computing children_hours, effective_hours, remaining_hours,
    -- total_hours, total_hours_spent, and delay_hours.  However, only
    -- children_hours and total_hours_spent need recomputation.

    -- These are new columns, the others existed before but created in another
    --  addon.

    ALTER TABLE project_task ADD COLUMN children_hours DOUBLE PRECISION;
    ALTER TABLE project_task ADD COLUMN total_hours_spent DOUBLE PRECISION;

    -- In _get_hours, ``task.total_hours_spent = task.effective_hours + task.children_hours``
    -- No need to bother to add 0.

    UPDATE project_task SET children_hours=0, total_hours_spent=effective_hours;

    ''')


@openupgrade.migrate()
def migrate(env, version):
    openupgrade.rename_columns(env.cr, COLUMN_RENAMES)
    create_and_populate_department(env.cr)
    create_and_populate_children(env.cr)
