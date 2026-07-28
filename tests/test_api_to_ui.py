"""Unit tests for api_to_ui.convert_api_to_ui.

Runs outside ComfyUI: node definitions are injected as fakes that mimic
the classic INPUT_TYPES/RETURN_TYPES class shape, so the conversion
logic (widget/socket split, control_after_generate, link compression,
serialization shape) is exercised without a live server.
"""

from __future__ import annotations

from typing import Any

from api_to_ui import convert_api_to_ui


class _FakeCheckpointLoader:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {"required": {"ckpt_name": (["a.safetensors", "b.safetensors"],)}}

    RETURN_TYPES = ("MODEL", "CLIP", "VAE")


class _FakeCLIPTextEncode:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "text": ("STRING", {"multiline": True}),
                "clip": ("CLIP",),
            }
        }

    RETURN_TYPES = ("CONDITIONING",)


class _FakeKSampler:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "model": ("MODEL",),
                "seed": ("INT", {"default": 0, "control_after_generate": True}),
                "steps": ("INT", {"default": 20}),
                "sampler_name": (["euler", "ddim"],),
                "positive": ("CONDITIONING",),
                "latent_image": ("LATENT",),
                "denoise": ("FLOAT", {"default": 1.0}),
            }
        }

    RETURN_TYPES = ("LATENT",)


class _FakeSaveImage:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE", {}),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ()


_MAPPINGS: dict[str, type] = {
    "CheckpointLoaderSimple": _FakeCheckpointLoader,
    "CLIPTextEncode": _FakeCLIPTextEncode,
    "KSampler": _FakeKSampler,
    "SaveImage": _FakeSaveImage,
}
_DISPLAY: dict[str, str] = {
    "CheckpointLoaderSimple": "Load Checkpoint",
    "CLIPTextEncode": "CLIP Text Encode (Prompt)",
    "KSampler": "KSampler",
    "SaveImage": "Save Image",
}


def _toy_prompt() -> dict[str, Any]:
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "b.safetensors"},
            "_meta": {"title": "Load Checkpoint"},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "a cat", "clip": ["4", 1]},
            "_meta": {"title": "my prompt"},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 25,
                "sampler_name": "ddim",
                "denoise": 0.7,
                "model": ["4", 0],
                "positive": ["6", 0],
                "latent_image": ["9", 0],  # dangling → skipped like the frontend
            },
        },
    }


def _convert(prompt: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    return convert_api_to_ui(
        prompt,
        node_class_mappings=_MAPPINGS,
        node_display_name_mappings=_DISPLAY,
    )


def _node(workflow: dict[str, Any], node_id: int) -> dict[str, Any]:
    return next(n for n in workflow["nodes"] if n["id"] == node_id)


def test_toplevel_shape() -> None:
    workflow, missing = _convert(_toy_prompt())
    assert missing == []
    assert workflow["version"] == 0.4
    assert workflow["groups"] == []
    assert workflow["config"] == {}
    assert workflow["last_node_id"] == 6
    assert [n["id"] for n in workflow["nodes"]] == [4, 6, 3]


def test_widgets_values_include_control_after_generate() -> None:
    workflow, _ = _convert(_toy_prompt())
    ksampler = _node(workflow, 3)
    # def order: seed, control_after_generate, steps, sampler_name, denoise
    assert ksampler["widgets_values"] == [42, "randomize", 25, "ddim", 0.7]


def test_unconnected_widget_slots_are_compressed() -> None:
    workflow, _ = _convert(_toy_prompt())
    ksampler = _node(workflow, 3)
    assert [i["name"] for i in ksampler["inputs"]] == ["model", "positive", "latent_image"]
    # widget-slot metadata never leaks into serialized socket inputs
    assert all("widget" not in i for i in ksampler["inputs"])
    # the dangling latent_image link was skipped
    assert ksampler["inputs"][2]["link"] is None


def test_connected_widget_slot_kept_with_widget_tag() -> None:
    prompt = _toy_prompt()
    # feed steps from a (fake) INT-producing output: reuse CLIP slot type
    # mismatch is rejected, so wire text (STRING widget) instead
    prompt["6"]["inputs"]["text"] = ["4", 1]  # CLIP → STRING is invalid, skipped
    workflow, _ = _convert(prompt)
    clip_node = _node(workflow, 6)
    # invalid type connection was skipped → widget slot compressed away
    assert [i["name"] for i in clip_node["inputs"]] == ["clip"]


def test_second_pass_link_ids() -> None:
    workflow, _ = _convert(_toy_prompt())
    # 3 valid links; the frontend's double connection pass leaves ids 4..6
    ids = [link[0] for link in workflow["links"]]
    assert ids == [4, 5, 6]
    assert workflow["last_link_id"] == 6


def test_link_target_slots_use_compressed_indices() -> None:
    workflow, _ = _convert(_toy_prompt())
    links = {(link[1], link[3], link[4]): link for link in workflow["links"]}
    # KSampler compressed inputs: model=0, positive=1, latent_image=2
    assert (4, 3, 0) in links  # checkpoint MODEL → model
    assert (6, 3, 1) in links  # conditioning → positive
    ksampler = _node(workflow, 3)
    assert ksampler["inputs"][0]["link"] == links[(4, 3, 0)][0]


def test_title_only_when_differs_from_display_name() -> None:
    workflow, _ = _convert(_toy_prompt())
    assert "title" not in _node(workflow, 4)  # matches display name
    assert _node(workflow, 6)["title"] == "my prompt"


def test_missing_node_types_skipped_and_reported() -> None:
    prompt = _toy_prompt()
    prompt["99"] = {"class_type": "NotARealNode", "inputs": {}}
    workflow, missing = _convert(prompt)
    assert missing == ["NotARealNode"]
    assert all(n["type"] != "NotARealNode" for n in workflow["nodes"])


def test_positions_are_arranged_in_level_columns() -> None:
    workflow, _ = _convert(_toy_prompt())
    loader, clip_node, ksampler = (_node(workflow, i) for i in (4, 6, 3))
    assert loader["pos"][0] < clip_node["pos"][0] < ksampler["pos"][0]
    assert loader["pos"] == [100.0, 130.0]  # margin, margin + title height


def test_ui_passthrough_and_properties() -> None:
    workflow, _ = _convert(_toy_prompt())
    assert _node(workflow, 4)["properties"]["Node name for S&R"] == "CheckpointLoaderSimple"
    assert _node(workflow, 3)["mode"] == 0
    assert _node(workflow, 3)["flags"] == {}
