import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

try:
    import numpy as np
except ImportError:
    np = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wallpaper_orientation as orientation


class OrientationTests(unittest.TestCase):
    def test_scores_and_clockwise_angles(self):
        for index, angle in enumerate((0, 90, 180, 270)):
            logits = [-10.0] * 4
            logits[index] = 10.0
            result = orientation._decision(logits)
            self.assertEqual(result.angle, angle)
            self.assertAlmostEqual(result.score, 1 / (1 + 3 * math.exp(-20)))

    def test_ambiguous_scores_abstain(self):
        self.assertIsNone(orientation._decision([1, 1, 1, 1]).angle)

    def test_invalid_logits_are_global_failure(self):
        for logits in ([1, 2, 3], [1, 2, 3, float('nan')]):
            with self.subTest(logits=logits), self.assertRaises(orientation.OrientationError):
                orientation._decision(logits)

    def test_predict_parses_ordered_results(self):
        payload = json.dumps([[90, 0.99], None, [None, 0.5]])
        completed = subprocess.CompletedProcess([], 0, payload, '')
        with mock.patch.object(orientation.subprocess, 'run', return_value=completed) as run:
            result = orientation.predict('model', ['a', 'b', 'c'], timeout=2)
        self.assertEqual(result, [orientation.Prediction(90, 0.99), None, orientation.Prediction(None, 0.5)])
        self.assertEqual(run.call_args.kwargs['timeout'], 2)

    def test_timeout_and_worker_errors_are_global(self):
        failures = [subprocess.TimeoutExpired('worker', 1), OSError('no interpreter')]
        for failure in failures:
            with self.subTest(failure=failure), mock.patch.object(orientation.subprocess, 'run', side_effect=failure), self.assertRaises(orientation.OrientationError):
                orientation.predict('model', ['image'], timeout=1)

    def test_invalid_worker_payload_is_global(self):
        for payload in ('{}', '[]', '[[45, 1]]', '[[0, NaN]]'):
            completed = subprocess.CompletedProcess([], 0, payload, '')
            with self.subTest(payload=payload), mock.patch.object(orientation.subprocess, 'run', return_value=completed), self.assertRaises(orientation.OrientationError):
                orientation.predict('model', ['image'])

    def test_real_worker_rejects_bad_model(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / 'bad.onnx'
            model.write_bytes(b'bad model')
            with self.assertRaises(orientation.OrientationError):
                orientation.predict(str(model), ['missing'])

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_preprocessing_composites_transparency_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alpha.png'
            Image.new('RGBA', (5, 7), (255, 0, 0, 0)).save(path)
            original = path.read_bytes()
            tensor = orientation._image_tensor(str(path))
            self.assertEqual(tensor.shape, (3, 384, 384))
            self.assertEqual(tensor.dtype, np.float32)
            self.assertTrue(tensor.flags.c_contiguous)
            np.testing.assert_allclose(tensor[:, 0, 0], (np.ones(3) - [.485, .456, .406]) / np.array([.229, .224, .225]), rtol=1e-6)
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_exif_transpose_occurs_before_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rotated.jpg'
            image = Image.new('RGB', (2, 4), 'red')
            for y in range(2, 4):
                for x in range(2):
                    image.putpixel((x, y), (0, 0, 255))
            exif = image.getexif()
            exif[274] = 6
            image.save(path, exif=exif, quality=100, subsampling=0)
            tensor = orientation._image_tensor(str(path))
            self.assertGreater(tensor[2, 192, 0], tensor[0, 192, 0])
            self.assertGreater(tensor[0, 192, -1], tensor[2, 192, -1])

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_worker_batches_and_isolates_bad_media(self):
        class Session:
            def __init__(self):
                self.sizes = []

            def run(self, names, feeds):
                self.assert_input(names, feeds)
                self.sizes.append(len(feeds['input']))
                return [np.tile([0, 0, 10, 0], (len(feeds['input']), 1))]

            def assert_input(self, names, feeds):
                assert names == ['output'] and set(feeds) == {'input'}
                assert feeds['input'].flags.c_contiguous

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.png'
            Image.new('L', (2, 2), 128).save(image)
            session = Session()
            result = orientation._infer(session, [str(image)] * 4 + [str(image) + '.missing', str(image)])
        self.assertEqual(session.sizes, [4, 1])
        self.assertEqual([item.angle if item else None for item in result], [180, 180, 180, 180, None, 180])

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_invalid_model_output_shape_is_global(self):
        session = mock.Mock()
        session.run.return_value = [np.zeros((1, 3))]
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.png'
            Image.new('RGB', (2, 2)).save(image)
            with self.assertRaises(orientation.OrientationError):
                orientation._infer(session, [str(image)])

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_image_size_and_decoded_pixel_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.png'
            Image.new('RGB', (4, 4)).save(image)
            with mock.patch.object(orientation, 'MAX_IMAGE_BYTES', 1):
                self.assertIsNone(orientation._image_tensor(str(image)))
            with mock.patch.object(orientation, 'MAX_IMAGE_PIXELS', 15):
                self.assertIsNone(orientation._image_tensor(str(image)))

    def test_missing_runtime_is_a_worker_failure(self):
        with mock.patch.object(orientation, '_model_bytes', return_value=b'verified'), \
                mock.patch.dict(sys.modules, {'onnxruntime': None}), \
                mock.patch.object(sys, 'stdin', io.StringIO('["model", ["image"]]')), self.assertRaises(ModuleNotFoundError):
            orientation._worker()

    def test_missing_runtime_is_reported_through_real_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'
            model.write_bytes(b'model')
            worker = root / 'wallpaper_orientation.py'
            source = Path(orientation.__file__).read_text()
            source = source.replace(orientation.MODEL_SHA256, hashlib.sha256(b'model').hexdigest())
            worker.write_text(source.replace('80_578_424', str(len(b'model'))))
            (root / 'onnxruntime.py').write_text('raise ImportError("missing runtime fixture")\n')
            with mock.patch.object(orientation, '__file__', str(worker)), self.assertRaises(orientation.OrientationError):
                orientation.predict(str(model), ['image'])

    def test_checksum_rejected_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / 'model'
            model.write_bytes(b'bad')
            with mock.patch.object(orientation, 'MODEL_SIZE', 3), \
                    mock.patch.object(sys, 'stdin', io.StringIO(json.dumps([str(model), ['image']]))), \
                    mock.patch.dict(sys.modules, {'onnxruntime': None}), self.assertRaises(orientation.OrientationError):
                orientation._worker()

    @unittest.skipIf(np is None, 'NumPy is optional')
    def test_real_subprocess_with_fake_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model.onnx'
            model.write_bytes(b'test model')
            worker = root / 'wallpaper_orientation.py'
            source = Path(orientation.__file__).read_text()
            source = source.replace(orientation.MODEL_SHA256, hashlib.sha256(b'test model').hexdigest())
            worker.write_text(source.replace('80_578_424', str(len(b'test model'))))
            (root / 'onnxruntime.py').write_text(
                'import numpy as np\n'
                'class SessionOptions: pass\n'
                'class InferenceSession:\n'
                ' def __init__(self, model, sess_options, providers):\n'
                '  assert model == b"test model" and providers == ["CPUExecutionProvider"]\n'
                '  assert sess_options.intra_op_num_threads == sess_options.inter_op_num_threads == 1\n'
                ' def run(self, outputs, feeds):\n'
                '  assert outputs == ["output"] and feeds["input"].shape[1:] == (3, 384, 384)\n'
                '  return [np.tile([0, 10, 0, 0], (len(feeds["input"]), 1))]\n'
            )
            image = root / 'image.png'
            Image.new('RGB', (4, 8), 'red').save(image)
            with mock.patch.object(orientation, '__file__', str(worker)):
                result = orientation.predict(str(model), [str(image), str(root / 'missing')], timeout=10)
            self.assertEqual(result[0].angle, 90)
            self.assertIsNone(result[1])

    def test_timeout_kills_and_reaps_real_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / 'pid'
            worker = root / 'worker.py'
            worker.write_text(
                'import os, pathlib, sys, threading\n'
                'sys.stdin.read()\n'
                f'pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
                'threading.Event().wait()\n'
            )
            with mock.patch.object(orientation, '__file__', str(worker)), self.assertRaises(orientation.OrientationError):
                orientation.predict('model', ['image'], timeout=1)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)


if __name__ == '__main__':
    unittest.main()
