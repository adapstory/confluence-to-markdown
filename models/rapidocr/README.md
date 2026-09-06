# Pinned RapidOCR models

These ONNX files are runtime inputs, not downloaded by the converter. They are
selected from the RapidOCR 3.9.2 model registry and verified by the converter
before the OCR engine is initialized.

| Local file | Upstream model | SHA-256 |
| --- | --- | --- |
| `detection.onnx` | `PP-OCRv6_det_small.onnx` | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| `classification.onnx` | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| `recognition-cyrillic.onnx` | `cyrillic_PP-OCRv5_rec_mobile.onnx` | `90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711` |

Canonical registry:
<https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml>

The files originate from PaddleOCR-compatible models distributed through the
RapidOCR model registry. Preserve this README when transferring the model
files to another machine.
