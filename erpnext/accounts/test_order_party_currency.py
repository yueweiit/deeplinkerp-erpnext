"""Order party details must return account currency before the first save."""
import unittest

import frappe
from erpnext.accounts.party import get_party_details


class TestOrderPartyCurrency(unittest.TestCase):
    def test_order_currency_uses_company_account_and_preserves_foreign_accounts(self):
        point = 'party_currency_' + frappe.generate_hash(length=8)
        frappe.db.savepoint(point)
        try:
            token = frappe.generate_hash(length=6).upper()
            company = frappe.get_doc({'doctype': 'Company', 'company_name': '_QA Currency ' + token,
                                     'abbr': token, 'default_currency': 'CNY', 'country': 'China',
                                     'create_chart_of_accounts_based_on': 'Standard Template',
                                     'chart_of_accounts': 'Standard'}).insert()
            supplier = frappe.get_doc({'doctype': 'Supplier', 'supplier_name': '_QA Currency Supplier ' + token,
                                      'supplier_type': 'Company',
                                      'supplier_group': frappe.db.get_value('Supplier Group', {'is_group': 0}, 'name')}).insert()
            details = get_party_details(party=supplier.name, party_type='Supplier', company=company.name,
                                       doctype='Purchase Order', currency='CNY')
            self.assertEqual(details.party_account_currency, 'CNY')
            foreign = frappe.get_doc({'doctype': 'Account', 'account_name': 'QA USD Payable ' + token,
                                      'parent_account': frappe.db.get_value('Account', company.default_payable_account, 'parent_account'),
                                      'company': company.name, 'account_type': 'Payable', 'account_currency': 'USD'}).insert()
            # Use a new party for a new request's account lookup, avoiding request-local caches.
            other = frappe.copy_doc(supplier)
            other.supplier_name = '_QA Foreign Supplier ' + token
            other.default_currency = 'USD'
            other.append('accounts', {'company': company.name, 'account': foreign.name})
            other.insert()
            details = get_party_details(party=other.name, party_type='Supplier', company=company.name,
                                       doctype='Purchase Order', currency='CNY')
            self.assertEqual(details.party_account_currency, 'USD')
            self.assertEqual(details.currency, 'USD')
            customer = frappe.get_doc({'doctype': 'Customer', 'customer_name': '_QA Currency Customer ' + token,
                                      'customer_type': 'Company',
                                      'customer_group': frappe.db.get_value('Customer Group', {'is_group': 0}, 'name'),
                                      'territory': frappe.db.get_value('Territory', {'is_group': 0}, 'name')}).insert()
            details = get_party_details(party=customer.name, party_type='Customer', company=company.name,
                                        doctype='Sales Order', currency='CNY')
            self.assertEqual(details.party_account_currency, 'CNY')
            receivable = frappe.get_doc({'doctype': 'Account', 'account_name': 'QA USD Receivable ' + token,
                                         'parent_account': frappe.db.get_value('Account', company.default_receivable_account, 'parent_account'),
                                         'company': company.name, 'account_type': 'Receivable', 'account_currency': 'USD'}).insert()
            other_customer = frappe.copy_doc(customer)
            other_customer.customer_name = '_QA Foreign Customer ' + token
            other_customer.default_currency = 'USD'
            other_customer.append('accounts', {'company': company.name, 'account': receivable.name})
            other_customer.insert()
            details = get_party_details(party=other_customer.name, party_type='Customer', company=company.name,
                                        doctype='Sales Order', currency='CNY')
            self.assertEqual(details.party_account_currency, 'USD')
            self.assertEqual(details.currency, 'USD')
        finally:
            frappe.db.rollback(save_point=point)
