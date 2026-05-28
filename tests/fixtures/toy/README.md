# toy fixture

Tiny image-processing workflow used by the test suite. Hand-crafted, uses
only ComfyUI **core** nodes (no custom nodes), no models, no checkpoints —
trivially public-safe and quick.

## Pipeline

```
LoadImage → ImageScale → ImageInvert → SaveImage
                            └────────→ PreviewImage   (added in B)
```

## Shape of the diff (B − A)

| Aspect          | Change                                                  |
| --------------- | ------------------------------------------------------- |
| added nodes     | `5` (PreviewImage), branching off ImageInvert           |
| removed nodes   | none                                                    |
| widget changes  | node `1` `image: "input.png" → "sample.png"`            |
|                 | node `2` `width: 512 → 1024`                            |
| moved-only      | node `4` (SaveImage) shifted down to make room          |
| added links     | one (`3 → 5`)                                           |
| removed links   | none                                                    |

Both serializations (`*_ui.json` = full editor format, `*_api.json` =
prompt format used by `/prompt`) describe the **same** logical graphs, so
all four (A-format, B-format) combinations should produce the same diff
verdict — that's the point of the format-adapter layer this test
exercises.
