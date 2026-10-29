import csv
import tempfile
import unittest
from pathlib import Path

from convert_pdf_to_csv import parse_pdf_rows
from guard_unchanged_pdf import needs_approval


class Page:
    def __init__(self, items):
        self.items = items

    def extract_text(self, visitor_text):
        for text, x, y in self.items:
            visitor_text(text, [], [1, 0, 0, 1, x, y], None, 10)


class ParserRegressionTests(unittest.TestCase):
    def test_legend_emitted_after_table_does_not_create_a_pub(self):
        class Reader:
            pages = [Page([('UK Area', 20, 647), ('ENGLAND', 20, 627),
                           ('DERBYSHIRE', 20, 608), ('Tavern', 150, 590),
                           ('Belper: Central', 320, 590), ('DE56 1UU', 460, 590),
                           ('Perm', 520, 590), ('2026 Q2', 560, 590),
                           ("'once a year'. New", 1105, 745),
                           ('and re-entries on the list are coloured gold.', 506, 728)]),
                     Page([('Thorn Tree Inn', 150, 774), ('Belper: Central', 320, 774),
                           ('DE56 1FF', 460, 774), ('Perm', 520, 774), ('2026 Q3', 560, 774)])]
        rows = parse_pdf_rows(Reader())
        self.assertEqual([r['pub_name'] for r in rows], ['Tavern', 'Thorn Tree Inn'])
        self.assertEqual(rows[0]['notes'], '')
        self.assertEqual(rows[1]['place_name'], 'Belper: Central')


class GuardTests(unittest.TestCase):
    def test_same_pdf_with_changed_listing_requires_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old.csv', Path(directory) / 'new.csv'
            old.write_text('pub_name,venue_id\nOld,v-1\n', encoding='utf-8')
            new.write_text('pub_name,venue_id\nNew,v-2\n', encoding='utf-8')
            self.assertTrue(needs_approval(b'pdf', b'pdf', old, new))
            self.assertFalse(needs_approval(b'pdf', b'new pdf', old, new))
            new.write_text('pub_name,venue_id\nOld,v-2\n', encoding='utf-8')
            self.assertFalse(needs_approval(b'pdf', b'pdf', old, new))


if __name__ == '__main__':
    unittest.main()

