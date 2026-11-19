"""CONV (TIA) = CONVERT (a proposal's spelling); a data type template one side does not carry is no difference. Synthetic data."""
import copy
import unittest

import diff
import fbd
import server


def lad(name, template, op='#X'):
    box = {'id': 'p1', 'type': 'box', 'call': 'instruction', 'name': name, 'instance': None, 'template': template,
           'power': ['EN', 'ENO'], 'inputs': [{'pin': 'IN1', 'operand': op}, {'pin': 'IN2', 'operand': '1'}],
           'outputs': [{'pin': 'OUT', 'operand': '#Y'}]}
    return {'elements': [box], 'rungs': [{'from': 'powerrail', 'path': ['p1'], 'to': None}]}


def fbd_model(name, template):
    return {'language': 'FBD', 'elements': [
        {'id': 'b1', 'type': 'instruction', 'call': 'instruction', 'name': name, 'instance': None, 'template': template,
         'inputs': [{'n': 1, 'pin': 'IN', 'operand': '#X', 'signal': None, 'neg': False}],
         'outputs': [{'pin': 'OUT', 'operand': '#Y', 'neg': False}]}]}


class TestConv(unittest.TestCase):
    def test_lad_convert_is_conv(self):
        self.assertEqual(diff.content_match(lad('CONVERT', 'Int to DInt'), lad('CONV', 'Int to DInt')), (True, []))
        self.assertEqual(diff.content_match(lad('convert', 'Int to DInt'), lad('CONV', 'Int to DInt')), (True, []))

    def test_fbd_convert_is_conv(self):
        self.assertEqual(fbd.content_match(fbd_model('CONVERT', 'Int to DInt'), fbd_model('CONV', 'Int to DInt')), (True, []))

    def test_missing_template_is_no_difference_but_a_different_one_is(self):
        self.assertEqual(diff.content_match(lad('DIV', 'Real'), lad('DIV', None)), (True, []))
        self.assertEqual(diff.content_match(lad('DIV', None), lad('DIV', 'Real')), (True, []))
        self.assertEqual(fbd.content_match(fbd_model('ADD', 'Real'), fbd_model('ADD', '')), (True, []))
        match, r = diff.content_match(lad('DIV', 'Real'), lad('DIV', 'Int'))
        self.assertFalse(match)
        self.assertIn('template Real → Int', r[0])
        self.assertFalse(diff.content_match(lad('DIV', None, '#A'), lad('DIV', 'Real'))[0])           # a real difference stays

    def test_inputs_are_not_changed_by_the_comparison(self):
        a = lad('CONVERT', 'Real')
        before = copy.deepcopy(a)
        diff.content_match(a, lad('CONV', None))
        self.assertEqual(a, before)

    def test_proposal_is_stored_as_conv(self):
        m = server._after_model(lad('CONVERT', 'Int to DInt'), 'LAD')
        self.assertEqual(m['elements'][0]['name'], 'CONV')
        m = server._after_model(fbd_model('CONVERT', 'Int to DInt'), 'FBD')
        self.assertEqual(m['elements'][0]['name'], 'CONV')


if __name__ == '__main__':
    unittest.main()
