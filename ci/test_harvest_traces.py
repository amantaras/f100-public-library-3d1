import unittest

from harvest_traces import application_id, mask


class HarvestTests(unittest.TestCase):
    def test_contact_details_are_masked_but_poison_control_is_kept(self):
        text = "Email me at owner@example.com or call +1 (555) 123-4567. ASPCA: (888) 426-4435."
        masked = mask(text)
        self.assertNotIn("owner@example.com", masked)
        self.assertNotIn("123-4567", masked)
        self.assertIn("(888) 426-4435", masked)

    def test_application_id_comes_from_connection_string(self):
        conn = "InstrumentationKey=abc;IngestionEndpoint=https://x/;ApplicationId=1234-app"
        self.assertEqual(application_id(conn), "1234-app")
        with self.assertRaises(ValueError):
            application_id("InstrumentationKey=abc")


if __name__ == "__main__":
    unittest.main()
