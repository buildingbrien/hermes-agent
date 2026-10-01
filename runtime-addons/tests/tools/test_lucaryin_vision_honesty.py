"""Lucaryin fold 0034 (F50): vision_analyze never reports an image it did not see.

Review of 2026-09-20..10-01, session mobile_3b0042fa_1789952463 (Sep 25):
- 5 of 9 vision_analyze calls on 1-2.3 MB screenshots returned success:true with
  text like 'appears as "[Unsupported Image]"' [370]-[374]; Merlin went on as if
  it had seen them.
- vision_analyze on a ~/Desktop screenshot returned "media file not found" [254]
  for a file that existed: macOS names screenshots "... at 8.14.32<U+202F>PM.png"
  (narrow no-break space) and the model retyped a plain space.

Hermetic: the aux vision call is stubbed, images are real files in tmp_path.
"""

import base64
import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import vision_tools
from tools.vision_tools import _analysis_says_unseen, vision_analyze_tool

UNSEEN = 'The image appears as "[Unsupported Image]", so I cannot describe its contents.'
SEEN = "A Lucaryin settings screen with the Always-approve toggle switched on for Merlin."


def _response(text):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.message.reasoning_content = None
    choice.message.reasoning = None
    resp.choices = [choice]
    return resp


def _png(path, size=(64, 48)):
    from PIL import Image
    Image.new("RGB", size, (200, 30, 30)).save(path, format="PNG")
    return path


def _noisy_png(path, size=(1800, 1400)):
    """A PNG well over 1 MB (random pixels do not compress), like a dense screenshot."""
    import os
    from PIL import Image
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, format="PNG")
    return path


# Real answers about what is ON SCREEN that the first version of the detector called "not
# seen" (review of PR #38: 5 of 6 such answers flagged, and 'No - I don't see a photo in the
# profile header; it shows the initials BC' went out as success:false after 2 model calls).
# Image-presence and upload checks in browser and desktop work read exactly like these.
UI_STATE_ANSWERS = [
    "I don't see a photo of a dog on this page; the results are all cats.",
    "The hero image did not load; the rest is a login form with email and password fields.",
    "I cannot see the image's bottom-right corner clearly, but the top half shows a bar chart.",
    "I do not see any photo in the avatar slot; it shows the initials BC.",
    "The Slack thread has one message from Dana that reads 'image could not be loaded'.",
    "No - I don't see a photo in the profile header; it shows the initials BC",
    'A chat window where one bubble reads "[Unsupported Image]".',
    "The image shows a Slack message: [Unsupported Image]",
    "No photo was provided for this contact, so the card shows the initials BC.",
    "No image is attached to this email in the inbox view; the paperclip icon is absent.",
    "I can't see any screenshot thumbnails in the sidebar; it is collapsed.",
    "I can't see the photo; the header shows the initials BC.",
    "I cannot see the image clearly; it is blurry, but it looks like a parking sign.",
    "The page shows a broken image icon where the logo should be; the image did not load.",
    "Yes. The upload worked: the image is shown in the preview pane next to the file name.",
    "I don't see an image.",
    "I don't see any photo.",
    "There is no photo in the header.",
    "No photo.",
    # Round 3 (review of #38): "no image in the message" / the placeholder as the INPUT must
    # end the sentence; these go on to describe an email or chat on screen.
    "There is no image in the message; the email body contains only text and a signature.",
    "No image in the message body — just three paragraphs of text.",
    "There is no image in your message thread shown here; the thread has two replies.",
    'The image appears as "[Unsupported Image]" in the Gmail preview pane, next to the subject '
    'line "Invoice 0921".',
    'It shows a Slack message that reads "[Unsupported Image]".',
]


class TestUnseenDetector:
    @pytest.mark.parametrize("text", [
        "[Unsupported Image]",
        '"[Unsupported Image]"',
        UNSEEN,
        "The provided image is displayed as [Unsupported Image].",
        "The screenshot came through as an [Unsupported Image] placeholder, so I can't read it.",
        "I can't see the image you attached. Could you send it again?",
        "I’m sorry, but I cannot view the screenshot.",
        "Sorry. I can't see the image.",
        "Unfortunately, I'm unable to view the image you've shared.",
        "I can't see the photo you sent, could you try again?",
        "I'm unable to open the attached screenshot.",
        "I don't see any image attached to your message.",
        "I can't view images right now.",
        # Round 3: the provider's placeholder said other ways (6 of 15 such variants were missed).
        'It appears as "[Unsupported Image]". I cannot see the content.',
        'It appears as "[Unsupported Image]".',
        "I'm unable to view the image; it shows up as \"[Unsupported Image]\".",
        'Based on what was provided, the image appears as "[Unsupported Image]" and I cannot view it.',
        'The image content is "[Unsupported Image]", which means I cannot see what it shows.',
        'The image you sent shows up as "[Unsupported Image]" on my side, so I am not able to see it.',
        "No image was provided in your message.",
        "No image was attached.",
        "There is no image in your message.",
        "",
        "   ",
    ])
    def test_unseen_answers(self, text):
        assert _analysis_says_unseen(text)

    @pytest.mark.parametrize("text", [
        SEEN,
        "The photo shows a receipt from Joe's Diner totalling $42.10.",
        "The image is a bar chart of monthly revenue.",
        # A long, real description of an app that itself displays the placeholder text.
        "This screenshot shows a chat window. " * 30 + 'One bubble reads "[Unsupported Image]".',
        # A polite opening followed by a long real description is a real answer.
        "I can't see the image you attached. " + "The dashboard lists twelve open invoices. " * 25,
    ] + UI_STATE_ANSWERS)
    def test_real_answers(self, text):
        assert not _analysis_says_unseen(text)


class TestVisionAnalyzeHonesty:
    @pytest.mark.asyncio
    async def test_unseen_twice_is_a_failure_not_a_success(self, tmp_path):
        img = _png(tmp_path / "shot.png")
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response(UNSEEN)) as llm:
            result = json.loads(await vision_analyze_tool(str(img), "what is on screen?", None))
        assert result["success"] is False
        assert "could not view this picture" in result["error"]
        assert llm.await_count == 2          # one smaller retry, then stop

    @pytest.mark.asyncio
    async def test_unseen_then_seen_after_the_smaller_retry(self, tmp_path):
        img = _png(tmp_path / "shot.png")
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   side_effect=[_response(UNSEEN), _response(SEEN)]):
            result = json.loads(await vision_analyze_tool(str(img), "what is on screen?", None))
        assert result["success"] is True
        assert SEEN in result["analysis"]

    @pytest.mark.asyncio
    async def test_empty_answer_is_a_failure(self, tmp_path):
        img = _png(tmp_path / "shot.png")
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response("")):
            result = json.loads(await vision_analyze_tool(str(img), "describe", None))
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_a_real_answer_is_unchanged(self, tmp_path):
        img = _png(tmp_path / "shot.png")
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response(SEEN)) as llm:
            result = json.loads(await vision_analyze_tool(str(img), "describe", None))
        assert result == {"success": True, "analysis": SEEN}
        assert llm.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [
        "No - I don't see a photo in the profile header; it shows the initials BC",
        "The hero image did not load; the rest is a login form with email and password fields.",
    ])
    async def test_an_image_presence_answer_is_a_success_after_one_call(self, tmp_path, answer):
        img = _png(tmp_path / "shot.png")
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response(answer)) as llm:
            result = json.loads(await vision_analyze_tool(str(img), "Is there a profile photo?", None))
        assert result == {"success": True, "analysis": answer}
        assert llm.await_count == 1

    @pytest.mark.asyncio
    async def test_a_big_screenshot_is_sent_downscaled(self, tmp_path):
        img = _noisy_png(tmp_path / "big.png")
        assert img.stat().st_size > vision_tools._LUCARYIN_VISION_PROACTIVE_BYTES
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response(SEEN)) as llm:
            result = json.loads(await vision_analyze_tool(str(img), "describe", None))
        assert result["success"] is True
        sent = llm.await_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]
        assert len(sent) <= vision_tools._LUCARYIN_VISION_TARGET_B64
        assert sent.startswith("data:image/jpeg;base64,")


class TestDesktopScreenshotNames:
    @pytest.mark.asyncio
    async def test_a_plain_space_finds_the_narrow_no_break_space_file(self, tmp_path):
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        real = _png(desktop / "Screenshot 2026-09-24 at 8.14.32 PM.png")
        typed = desktop / "Screenshot 2026-09-24 at 8.14.32 PM.png"
        assert not typed.exists()
        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   return_value=_response(SEEN)):
            result = json.loads(await vision_analyze_tool(str(typed), "describe", None))
        assert result["success"] is True, result
        assert real.exists()

    @pytest.mark.asyncio
    async def test_two_equivalent_names_are_never_guessed(self, tmp_path):
        from tools.image_source import ResolveContext, SourceNotFound, resolve_image_source
        _png(tmp_path / "a b.png")
        _png(tmp_path / "a b.png")
        with pytest.raises(SourceNotFound):
            await resolve_image_source(str(tmp_path / "a b.png"), ResolveContext())

    @pytest.mark.asyncio
    async def test_a_missing_file_is_still_not_found(self, tmp_path):
        from tools.image_source import ResolveContext, SourceNotFound, resolve_image_source
        with pytest.raises(SourceNotFound):
            await resolve_image_source(str(tmp_path / "nope.png"), ResolveContext())
