import json
import sqlite3
import unittest
from app.data import connect, initialize
from app.evidence import prepare_patient, review_drug, visible, cutoff_value


class EvidenceSafetyTests(unittest.TestCase):
    def setUp(self):
        # Use empty source schemas, and explicitly synthetic records isolated from real demo data.
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        with connect() as source:
            schemas=source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND (name LIKE 'hosp_%' OR name LIKE 'icu_%')").fetchall()
        for schema in schemas:
            self.conn.execute(schema[0])
        initialize(self.conn)
        self.insert('hosp_patients',source_row=1,subject_id='1',gender='F',anchor_age='40')
        self.insert('hosp_patients',source_row=2,subject_id='2',gender='M',anchor_age='50')
        self.insert('hosp_admissions',source_row=1,subject_id='1',hadm_id='10',admittime='2150-01-01 00:00:00',dischtime='2150-01-05 00:00:00')
        self.insert('hosp_admissions',source_row=2,subject_id='1',hadm_id='11',admittime='2150-02-01 00:00:00',dischtime='2150-02-05 00:00:00')
        self.insert('hosp_emar',source_row=1,subject_id='1',hadm_id='10',emar_id='1-1',medication='Vancomycin',charttime='2150-01-02 10:00:00',storetime='2150-01-02 10:01:00',event_txt='Administered')
        self.insert('hosp_emar',source_row=2,subject_id='1',hadm_id='10',emar_id='1-2',medication='Vancomycin',charttime='2150-01-02 12:00:00',storetime='2150-01-04 12:00:00',event_txt='Not Given')
        self.insert('hosp_emar',source_row=3,subject_id='2',hadm_id='20',emar_id='2-1',medication='Vancomycin',charttime='2150-01-02 10:00:00',storetime='2150-01-02 10:01:00',event_txt='Administered')
        self.insert('hosp_prescriptions',source_row=1,subject_id='1',hadm_id='10',drug='Amoxicillin',starttime='2150-01-01 00:00:00')
        self.insert('hosp_d_icd_diagnoses',source_row=1,icd_code='9950',icd_version='9',long_title='Other anaphylactic reaction')
        self.insert('hosp_diagnoses_icd',source_row=1,subject_id='1',hadm_id='10',icd_code='9950',icd_version='9')
        self.insert('icu_d_items',source_row=1,itemid='220050',label='Arterial Blood Pressure systolic',category='Routine Vital Signs')
        for row,admission in [(1,'10'),(2,'11')]:
            self.insert('icu_chartevents',source_row=row,subject_id='1',hadm_id=admission,itemid='220050',charttime='2150-01-02 10:10:00',storetime='2150-01-02 10:11:00',value='70',valueuom='mmHg')
        self.conn.commit()

    def insert(self,table,**values):
        columns=[r[1] for r in self.conn.execute(f'PRAGMA table_info({table})')]
        self.conn.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in columns)})',[values.get(c,'') for c in columns])

    def tearDown(self):
        self.conn.close()

    def test_prescription_is_not_administration(self):
        review=review_drug(prepare_patient('1',conn=self.conn),'Amoxicillin')
        self.assertEqual(review['counts']['ordered_only'],1)
        self.assertEqual(review['counts']['administration_recorded'],0)
        self.assertEqual(review['allergy_status'],'unable_to_ascertain')

    def test_no_causality_from_anaphylaxis_or_low_pressure(self):
        result=review_drug(prepare_patient('1',conn=self.conn),'Vancomycin')
        self.assertEqual(result['patient_context'][0]['drug_link'],'unlinked')
        self.assertEqual(result['drug_causality'],'not_assessed')
        self.assertEqual(len(result['vital_context']),1)  # excludes another admission
        self.assertEqual(result['counts']['not_given'],1)

    def test_as_of_excludes_delayed_storage_and_future_diagnoses(self):
        b=prepare_patient('1','2150-01-03T00:00:00',conn=self.conn)
        self.assertEqual(len(b['exposures']),1)
        self.assertEqual(b['diagnoses'],[])
        self.assertEqual(len(b['admissions']),1)
        self.assertEqual(b['admissions'][0]['dischtime'],'')
        self.assertEqual(b['excluded']['prescriptions'],1)

    def test_as_of_cache_is_separate_and_patient_isolated(self):
        full=prepare_patient('1',conn=self.conn)
        early=prepare_patient('1','2150-01-03',conn=self.conn)
        self.assertNotEqual(full['cutoff'],early['cutoff'])
        self.assertEqual(len(full['exposures']),3)
        self.assertEqual(len(early['exposures']),1)
        self.assertEqual(prepare_patient('1',conn=self.conn),full)
        self.assertTrue(all(x['source']['row']!=3 for x in full['exposures'] if x['kind']=='emar'))

    def test_blank_status_and_blank_availability_do_not_confirm(self):
        self.insert('hosp_emar',source_row=4,subject_id='1',hadm_id='10',emar_id='1-4',medication='UnknownDrug',charttime='2150-01-02 10:00:00')
        result=review_drug(prepare_patient('1',conn=self.conn),'UnknownDrug')
        self.assertEqual(result['counts']['uncertain'],1)
        self.assertFalse(visible({'charttime':'2150-01-01 00:00:00','storetime':''},'2150-01-03 00:00:00'))

    def test_no_fuzzy_causality_or_cross_reactivity(self):
        b=prepare_patient('1',conn=self.conn)
        self.assertEqual(review_drug(b,'Vancomycin')['total_exposures'],2)
        self.assertEqual(review_drug(b,'vanco')['total_exposures'],0)
        self.assertEqual(review_drug(b,'  VANCOMYCIN  ')['total_exposures'],2)

    def test_invalid_time_rejected(self):
        for value in ['today','2150-01-01T00:00:00+09:00']:
            with self.assertRaises(ValueError):
                cutoff_value(value)


if __name__=='__main__':
    unittest.main()
