"""Company-scoped cancellation guards without writing ledger or fixture records."""

import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import frappe

from erpnext.accounts import general_ledger as gl


class TestReverseGLCompanyFreeze(unittest.TestCase):
	def setUp(self):
		stack = ExitStack()
		self.addCleanup(stack.close)
		language = frappe.local.lang
		frappe.local.lang = "en"
		self.addCleanup(setattr, frappe.local, "lang", language)
		self.companies = {
			"Open Company": {"accounts_frozen_till_date": None, "role_allowed_for_frozen_entries": None},
			"Frozen Company": {
				"accounts_frozen_till_date": "2026-09-30",
				"role_allowed_for_frozen_entries": "Frozen Accounts Editor",
			},
		}
		self.closing_dates = {}
		self.entries = [
			frappe._dict(
				company="Open Company",
				posting_date="2026-03-20",
				voucher_type="Journal Entry",
				voucher_no="TEST-GL-REVERSAL",
				is_opening="No",
				account="Test Bank",
				debit=100,
				credit=0,
				debit_in_account_currency=20,
				credit_in_account_currency=0,
				debit_in_transaction_currency=25,
				credit_in_transaction_currency=0,
			),
			frappe._dict(
				company="Open Company",
				posting_date="2026-03-20",
				voucher_type="Journal Entry",
				voucher_no="TEST-GL-REVERSAL",
				is_opening="No",
				account="Test Expense",
				debit=0,
				credit=100,
				debit_in_account_currency=0,
				credit_in_account_currency=20,
				debit_in_transaction_currency=0,
				credit_in_transaction_currency=25,
			),
		]
		self.original_get_value = frappe.db.get_value
		self.lookup = stack.enter_context(patch.object(frappe.db, "get_value", side_effect=self.get_value))
		self.roles = stack.enter_context(patch.object(frappe, "get_roles", return_value=[]))
		self.session = frappe._dict(user="ledger-test@example.com")
		stack.enter_context(patch.object(frappe, "session", self.session))
		stack.enter_context(patch.object(gl, "is_immutable_ledger_enabled", return_value=False))
		self.payment_ledger = stack.enter_context(patch.object(gl, "create_payment_ledger_entry"))
		self.period_check = stack.enter_context(patch.object(gl, "validate_accounting_period"))
		self.cancel_gl = stack.enter_context(patch.object(gl, "set_as_cancel"))
		self.make_entry = stack.enter_context(patch.object(gl, "make_entry"))

	def get_value(self, doctype, filters, fieldname, *args, **kwargs):
		if doctype == "Company":
			# MariaDB compares nonnumeric company names equal to numeric 0. Model
			# the unrelated frozen row returned when False is passed as company.
			company = filters if isinstance(filters, str) else "Frozen Company"
			return self.companies[company][fieldname]
		if doctype == "Period Closing Voucher":
			return self.closing_dates.get(filters["company"])
		# Date formatting may read language metadata; keep those read-only lookups native.
		return self.original_get_value(doctype, filters, fieldname, *args, **kwargs)

	def set_company(self, company):
		for entry in self.entries:
			entry.company = company

	def company_lookups(self):
		return [
			call.args[1] if len(call.args) > 1 else call.kwargs.get("filters")
			for call in self.lookup.call_args_list
			if (call.args[0] if call.args else call.kwargs.get("doctype")) == "Company"
		]

	def assert_company_checked(self, company):
		company_lookups = self.company_lookups()
		self.assertTrue(company_lookups)
		self.assertEqual(set(company_lookups), {company})

	def test_other_company_freeze_does_not_block_cancellation(self):
		gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Open Company")
		self.assertEqual(self.make_entry.call_count, 2)
		self.cancel_gl.assert_called_once_with("Journal Entry", "TEST-GL-REVERSAL")
		self.period_check.assert_called_once_with(self.entries)

	def test_real_company_freeze_blocks_entries_before_and_on_cutoff(self):
		self.set_company("Frozen Company")
		for date in ("2026-03-20", "2026-09-30"):
			with self.subTest(date=date):
				for entry in self.entries:
					entry.posting_date = date
				with self.assertRaises(frappe.ValidationError):
					gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Frozen Company")
		self.make_entry.assert_not_called()
		self.cancel_gl.assert_not_called()

	def test_entries_after_company_freeze_are_allowed(self):
		self.set_company("Frozen Company")
		for entry in self.entries:
			entry.posting_date = "2026-10-01"
		gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Frozen Company")
		self.assertEqual(self.make_entry.call_count, 2)

	def test_configured_frozen_entry_role_remains_effective(self):
		self.set_company("Frozen Company")
		self.roles.return_value = ["Frozen Accounts Editor"]
		gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Frozen Company")
		self.assertEqual(self.make_entry.call_count, 2)

	def test_administrator_cannot_bypass_real_company_freeze(self):
		self.set_company("Frozen Company")
		self.session.user = "Administrator"
		self.roles.return_value = ["Frozen Accounts Editor"]
		with self.assertRaises(frappe.ValidationError):
			gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Frozen Company")
		self.make_entry.assert_not_called()
		self.cancel_gl.assert_not_called()

	def test_advance_adjustment_flag_is_forwarded(self):
		self.set_company("Frozen Company")
		gl.make_reverse_gl_entries(self.entries, adv_adj=True)
		self.assertEqual(self.company_lookups(), [])
		self.payment_ledger.assert_called_once_with(
			self.entries, cancel=1, adv_adj=True, update_outstanding="Yes", partial_cancel=False
		)
		self.assertEqual(self.make_entry.call_count, 2)

	def test_submitted_company_closing_still_blocks_reversal(self):
		self.closing_dates["Open Company"] = "2026-09-30"
		with self.assertRaises(frappe.ValidationError) as rejected:
			gl.make_reverse_gl_entries(self.entries)
		self.assertIn("Books have been closed", str(rejected.exception))
		self.make_entry.assert_not_called()
		self.cancel_gl.assert_not_called()

	def test_other_company_closing_does_not_block_reversal(self):
		self.closing_dates["Frozen Company"] = "2026-09-30"
		gl.make_reverse_gl_entries(self.entries)
		self.assert_company_checked("Open Company")
		self.assertEqual(self.make_entry.call_count, 2)

	def test_advance_adjustment_keeps_period_closing_guard(self):
		self.closing_dates["Frozen Company"] = "2026-09-30"
		self.set_company("Frozen Company")
		with self.assertRaises(frappe.ValidationError) as rejected:
			gl.make_reverse_gl_entries(self.entries, adv_adj=True)
		self.assertIn("Books have been closed", str(rejected.exception))
		self.make_entry.assert_not_called()
		self.cancel_gl.assert_not_called()

	def test_explicit_reversal_date_preserves_existing_closing_date_semantics(self):
		self.closing_dates["Open Company"] = "2026-09-30"
		gl.make_reverse_gl_entries(self.entries, posting_date="2026-10-01")
		self.assertEqual(self.make_entry.call_count, 2)
		for call in self.make_entry.call_args_list:
			self.assertEqual(call.args[0].posting_date, "2026-10-01")

	def test_reversal_preserves_company_and_swaps_all_currency_amounts(self):
		original = copy.deepcopy(self.entries)
		gl.make_reverse_gl_entries(self.entries)
		self.assertEqual(self.entries, original)
		self.assertEqual(self.make_entry.call_count, 2)
		for entry, call in zip(original, self.make_entry.call_args_list, strict=True):
			reversed_entry = call.args[0]
			self.assertEqual(reversed_entry.company, entry.company)
			self.assertEqual(reversed_entry.account, entry.account)
			for suffix in ("", "_in_account_currency", "_in_transaction_currency"):
				self.assertEqual(reversed_entry["debit" + suffix], entry["credit" + suffix])
				self.assertEqual(reversed_entry["credit" + suffix], entry["debit" + suffix])
			self.assertEqual(call.args[1:], (False, "Yes"))

	def test_accounting_period_guard_still_blocks_before_ledger_cancellation(self):
		self.period_check.side_effect = frappe.ValidationError("Closed accounting period")
		with self.assertRaisesRegex(frappe.ValidationError, "Closed accounting period"):
			gl.make_reverse_gl_entries(self.entries)
		self.lookup.assert_not_called()
		self.make_entry.assert_not_called()
		self.cancel_gl.assert_not_called()
