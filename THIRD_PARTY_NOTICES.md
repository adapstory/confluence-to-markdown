# Third-party notices

The repository redistributes three OCR model files selected through the
RapidOCR 3.9.2 model registry. RapidOCR and PaddleOCR publish their work under
the Apache License 2.0. A copy is available at
[`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt).

- RapidOCR: <https://github.com/RapidAI/RapidOCR>
- PaddleOCR: <https://github.com/PaddlePaddle/PaddleOCR>
- Canonical RapidOCR model registry:
  <https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml>

## Redistributed model files

| Local file | Upstream model | SHA-256 |
| --- | --- | --- |
| `models/rapidocr/detection.onnx` | `PP-OCRv6_det_small.onnx` | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| `models/rapidocr/classification.onnx` | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| `models/rapidocr/recognition-cyrillic.onnx` | `cyrillic_PP-OCRv5_rec_mobile.onnx` | `90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711` |

The models are unmodified. Local filenames are shortened for a stable runtime
contract.
