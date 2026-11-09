"""Native shell geometry and the UI/agent shutdown boundary; no microphone."""

import asyncio
import queue
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import AppKit as AK
from Foundation import NSMakeRect
import Quartz as Q

from src.ui.app import _run_agent
from src.ui.notch import NotchWindow, notch_path


class NotchTests(unittest.TestCase):
    def build_notch(self):
        AK.NSApplication.sharedApplication()
        notch = NotchWindow.alloc().init()
        notch.build()
        self.addCleanup(notch.close)
        return notch

    def test_shape_stays_top_centered_and_rounds_the_bottom_corners(self):
        for width, height, top, bottom in ((293, 32, 6, 10), (197, 32, 6, 10)):
            with self.subTest(width=width):
                path = notch_path(293, 32, width, height, top, bottom)
                bounds = Q.CGPathGetPathBoundingBox(path)
                self.assertEqual(bounds, NSMakeRect((293-width)/2, 32-height, width, height))
                self.assertTrue(Q.CGPathContainsPoint(path, None, (146.5, 31), False))
                self.assertTrue(Q.CGPathContainsPoint(path, None, (146.5, 33-height), False))
                self.assertFalse(Q.CGPathContainsPoint(
                    path, None, ((293-width)/2 + top + 1, 33-height), False,
                ))
        hidden = notch_path(293, 32, 0, 0, 0, 0)
        self.assertFalse(Q.CGPathContainsPoint(hidden, None, (146.5, 31), False))

    def test_native_panel_tracks_the_notched_screen_and_reverses_animation(self):
        AK.NSApplication.sharedApplication()
        notch = NotchWindow.alloc().init()
        notch.build()
        self.addCleanup(notch.close)
        screens = AK.NSScreen.screens()
        screen = next((s for s in screens if s.safeAreaInsets().top > 0), screens[0])
        frame = screen.frame()
        panel = notch.panel.frame()
        self.assertEqual(panel.origin.y + panel.size.height, frame.origin.y + frame.size.height)
        self.assertEqual(panel.origin.x + panel.size.width/2, frame.origin.x + frame.size.width/2)
        self.assertTrue(notch.panel.ignoresMouseEvents())
        self.assertFalse(notch.panel.isKeyWindow())
        notch.set_expanded(True)
        self.assertTrue(notch.expanded)
        self.assertEqual(Q.CGPathGetPathBoundingBox(notch.shape.path()).size.height, notch.height)
        self.assertEqual(notch.height, notch.closed_height or 32)
        self.assertGreaterEqual(notch.width - (notch.closed_width or 197), 96)
        self.assertLess(notch.width - (notch.closed_width or 197), 98)
        self.assertIsNotNone(notch.moon.contents())
        notch.set_expanded(False)
        self.assertFalse(notch.expanded)
        self.assertEqual(Q.CGPathGetPathBoundingBox(notch.shape.path()).size.height, notch.closed_height)

    def test_display_change_without_a_notch_collapses_to_nothing(self):
        AK.NSApplication.sharedApplication()
        notch = NotchWindow.alloc().init()
        notch.build()
        self.addCleanup(notch.close)
        screen = SimpleNamespace(
            frame=lambda: NSMakeRect(-1920, 100, 1920, 1080),
            safeAreaInsets=lambda: SimpleNamespace(top=0),
            auxiliaryTopLeftArea=lambda: None,
            auxiliaryTopRightArea=lambda: None,
            backingScaleFactor=lambda: 1,
        )
        with patch.object(AK, "NSScreen", SimpleNamespace(screens=lambda: [screen])):
            notch.screenChanged_(None)
        self.assertEqual(notch.panel.frame().origin, (-1232, 724))
        self.assertEqual(notch.closed_width, 0)
        self.assertEqual(notch.closed_height, 0)
        self.assertEqual(Q.CGPathGetPathBoundingBox(notch.shape.path()).size, (0, 0))
        notch.set_expanded(True)
        notch._draw(animate=False)
        self.assertEqual(Q.CGPathGetPathBoundingBox(notch.shape.path()).size, (294, 32))

    def test_visualizer_uses_the_speaking_source_and_silence_settles(self):
        AK.NSApplication.sharedApplication()
        notch = NotchWindow.alloc().init()
        notch.build()
        self.addCleanup(notch.close)
        notch.set_expanded(True)
        now = time.monotonic()
        with patch("src.ui.notch.time.monotonic", return_value=now):
            notch.last_update = now - 0.03
            notch.set_audio("user", (0.2, 0.4, 0.8, 0.6, 0.3), now)
            notch.update_audio()
            self.assertGreater(max(notch.levels), 0)
            self.assertFalse(notch.speaking)
            self.assertTrue(Q.CGColorEqualToColor(notch.visualizer.colors()[0], AK.NSColor.whiteColor().CGColor()))
            notch.set_audio("luna", (0.3, 0.5, 0.6, 0.8, 0.4), now)
            notch.update_audio()
            self.assertTrue(notch.speaking)
            self.assertFalse(Q.CGColorEqualToColor(*notch.visualizer.colors()))
            notch.set_audio("luna", None, now)
            notch.update_audio()
            self.assertFalse(notch.speaking)
        previous = max(notch.levels)
        with patch("src.ui.notch.time.monotonic", return_value=now + 1):
            notch.update_audio()
            self.assertLess(max(notch.levels), previous)
        notch.set_expanded(False)
        self.assertEqual(notch.levels, (0,) * 5)

    def test_hover_opens_without_focus_then_leaving_restores_voice(self):
        notch = self.build_notch()
        frame = notch.panel.frame()
        inside = (frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height - 8)
        outside = (frame.origin.x - 20, frame.origin.y - 20)
        notch.update_hover(inside, 10)
        self.assertEqual(notch.mode, "closed")
        notch.update_hover(inside, 10.19)
        self.assertEqual(notch.mode, "compose")
        self.assertTrue(notch.panel.canBecomeKeyWindow())
        self.assertFalse(notch.panel.isKeyWindow())
        self.assertFalse(notch.panel.ignoresMouseEvents())
        self.assertTrue(notch.pin_button.isHidden())
        self.assertEqual(notch.visualizer.opacity(), 0)
        notch.chat.prompt.setString_("A draft that should survive")
        notch.set_expanded(True)
        self.assertEqual(notch.mode, "compose")
        notch.update_hover(outside, 11)
        notch.update_hover(outside, 11.36)
        self.assertEqual(notch.mode, "voice")
        self.assertTrue(notch.panel.ignoresMouseEvents())
        self.assertEqual(notch.chat.prompt.string(), "A draft that should survive")

    def test_send_streams_mock_reply_and_pin_survives_voice_end_and_pointer_exit(self):
        notch = self.build_notch()
        notch.hovered = True
        notch._draw(animate=False)
        notch.chat.prompt.setString_("Let's plan a focused afternoon.")
        notch.chat.send_(None)
        self.assertEqual(notch.mode, "chat")
        self.assertEqual(len(notch.chat.messages), 2)
        self.assertEqual(notch.chat.messages[0].role, "user")
        self.assertEqual(notch.chat.prompt.string(), "")
        notch._draw(animate=False)
        self.assertFalse(notch.pin_button.isHidden())
        self.assertFalse(notch.chat.send_button.isEnabled())
        bubble, reply, start = notch.chat.pending
        notch.chat.tick(start + 0.2)
        self.assertTrue(reply.startswith(bubble.text))
        self.assertLess(len(bubble.text), len(reply))
        notch.chat.prompt.setString_("The next draft")
        notch.chat.send_(None)
        self.assertEqual(len(notch.chat.messages), 2)
        notch.chat.tick(start + 10)
        self.assertEqual(bubble.text, reply)
        self.assertIsNone(notch.chat.pending)
        self.assertTrue(notch.chat.send_button.isEnabled())
        notch.togglePin_(None)
        notch.set_expanded(True)
        notch.set_expanded(False)
        frame = notch.panel.frame()
        outside = (frame.origin.x - 20, frame.origin.y - 20)
        notch.update_hover(outside, 20)
        notch.update_hover(outside, 20.36)
        self.assertEqual(notch.mode, "chat")
        self.assertTrue(notch.pinned)
        notch.dismiss()
        self.assertEqual(notch.mode, "closed")
        self.assertEqual(notch.chat.prompt.string(), "The next draft")
        self.assertEqual(len(notch.chat.messages), 2)
        inside = (frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height - 8)
        notch.update_hover(inside, 21)
        notch.update_hover(inside, 22)
        self.assertEqual(notch.mode, "closed")
        notch.update_hover(outside, 23)
        notch.update_hover(inside, 24)
        notch.update_hover(inside, 24.2)
        self.assertEqual(notch.mode, "chat")

    def test_chat_wraps_long_messages_and_preserves_scroll_position(self):
        notch = self.build_notch()
        notch.chat.prompt.setString_("   \n ")
        notch.chat.send_(None)
        self.assertFalse(notch.chat.messages)
        for index in range(12):
            notch.chat.append("user" if index % 2 == 0 else "luna", "Long message. " * 25)
        notch.open_chat()
        notch._draw(animate=False)
        notch.chat.layout_messages(force_bottom=True)
        viewport = notch.chat.scroll.contentView().bounds()
        self.assertGreater(viewport.origin.y, 0)
        self.assertGreater(notch.chat.document.frame().size.height, viewport.size.height)
        for bubble in notch.chat.messages:
            self.assertGreater(bubble.frame().size.height, 38)
            self.assertGreaterEqual(bubble.frame().origin.x, 0)
            self.assertLessEqual(bubble.frame().origin.x + bubble.frame().size.width, viewport.size.width)
        notch.chat.document.scrollPoint_((0, 0))
        notch.chat.messages[-1].text += "More incoming text. " * 8
        notch.chat.layout_messages()
        self.assertEqual(notch.chat.scroll.contentView().bounds().origin.y, 0)

    def test_native_text_cell_fits_the_entire_message_and_enter_supports_newlines(self):
        notch = self.build_notch()
        text = "Could you help me plan a focused afternoon?"
        bubble = notch.chat.append("user", text)
        bubble.measure(410)
        cell = bubble.label.cell().cellSizeForBounds_(NSMakeRect(0, 0, bubble.label.frame().size.width, 100_000))
        self.assertLessEqual(cell.height, bubble.label.frame().size.height)
        notch.chat.messages.clear()
        notch.chat.prompt.setString_("A first line")
        notch.chat.prompt.setSelectedRange_((len(notch.chat.prompt.string()), 0))
        event = SimpleNamespace(modifierFlags=lambda: AK.NSEventModifierFlagShift)
        with patch.object(AK, "NSApplication", SimpleNamespace(
            sharedApplication=lambda: SimpleNamespace(currentEvent=lambda: event),
        )):
            notch.chat.prompt.insertNewline_(None)
        self.assertIn("\n", notch.chat.prompt.string())
        self.assertFalse(notch.chat.messages)
        notch.chat.prompt.insertNewline_(None)
        self.assertEqual(len(notch.chat.messages), 2)


class AppLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_is_interactive_until_stopped_and_never_connects_the_agent(self):
        commands, events = queue.Queue(), queue.Queue()
        with patch("src.agent.conversation.run_conversation") as conversation:
            task = asyncio.create_task(_run_agent(commands, events, preview=True))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertTrue(events.empty())
            commands.put(None)
            await asyncio.wait_for(task, 2)
            conversation.assert_not_called()

    async def test_stop_is_forwarded_to_the_agent_without_installing_thread_signals(self):
        commands, events = queue.Queue(), queue.Queue()
        running = asyncio.Event()

        async def conversation(*, stopped, on_activity, on_audio):
            on_activity(True)
            on_audio(("user", (0.5,) * 5, 1.0))
            running.set()
            await stopped.wait()
            on_activity(False)

        with patch("src.agent.conversation.run_conversation", side_effect=conversation):
            task = asyncio.create_task(_run_agent(commands, events))
            await asyncio.wait_for(running.wait(), 2)
            self.assertTrue(events.get_nowait())
            self.assertEqual(events.get_nowait(), ("user", (0.5,) * 5, 1.0))
            commands.put(None)
            await asyncio.wait_for(task, 2)
            self.assertFalse(events.get_nowait())

    async def test_startup_failure_releases_the_shutdown_waiter(self):
        with patch("src.agent.conversation.run_conversation", side_effect=ValueError("missing key")):
            with self.assertRaisesRegex(ValueError, "missing key"):
                await asyncio.wait_for(_run_agent(queue.Queue(), queue.Queue()), 2)
