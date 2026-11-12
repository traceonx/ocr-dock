"""Real CPU inference: labels are known from independent image generation."""
from pathlib import Path
import unittest
from ocr_dock.config import Settings
from ocr_dock.engine import Engine, load_model


class RealModelTests(unittest.TestCase):
    def test_real_digits_and_mixed_characters(self):
        model=load_model(2)
        self.assertEqual(model.session.get_providers(),['CPUExecutionProvider'])
        fixtures=Path(__file__).parent/'fixtures'
        for name,expected in [('digits-49348.png','49348'),('mixed-k7Mz.png','k7Mz')]:
            with self.subTest(name=name):
                text,details=model.classification_box((fixtures/name).read_bytes())
                self.assertEqual(text,expected)
                self.assertEqual(len(details),len(expected))
                self.assertTrue(all(.25<=c['conf']<=1 for c in details))
