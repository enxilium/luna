"""Glass island layers: frosted backdrop, black notch band, edge light, corona glow and the Diamond Ring mark."""

import math

import AppKit as AK
from Foundation import NSMakeRect
import Quartz as Q


def rgb(r, g, b, a=1.0):
    return AK.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a).CGColor()


CLEAR = rgb(0, 0, 0, 0)
WHITE = rgb(1, 1, 1)
VIOLET = rgb(0.65, 0.58, 1)
LILAC = rgb(0.9, 0.87, 1)
AMBER = rgb(1, 0.77, 0.42)
BAND_FADE = 66  # Points below the hardware band over which black turns into glass.
PAD = 12  # One outer margin between the island's visible edge and its controls, on every side.


def _bare(layer):
    layer.setActions_({key: AK.NSNull.null() for key in (
        "position", "bounds", "frame", "path", "opacity", "locations", "colors", "shadowPath",
        "shadowOpacity", "strokeColor", "fillColor", "contents", "hidden",
    )})
    return layer


class Surface:
    """Everything behind the controls. The camera housing is opaque, so glass only starts below the band."""

    def __init__(self, view):
        bounds = view.bounds()
        self.aura = _bare(Q.CALayer.layer())
        self.aura.setShadowColor_(rgb(0.37, 0.29, 0.92))
        # A soft light that pools below the island rather than a halo around it.
        self.aura.setShadowRadius_(22)
        self.aura.setShadowOffset_((0, -14))
        self.aura.setShadowOpacity_(0)
        view.layer().addSublayer_(self.aura)
        self.effect = AK.NSVisualEffectView.alloc().initWithFrame_(bounds)
        self.effect.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
        self.effect.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
        self.effect.setState_(AK.NSVisualEffectStateActive)
        self.effect.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameVibrantDark))
        self.effect.setWantsLayer_(True)
        self.effect_mask = _bare(Q.CAShapeLayer.layer())
        self.effect.layer().setMask_(self.effect_mask)
        view.addSubview_(self.effect)
        self.chrome = AK.NSView.alloc().initWithFrame_(bounds)
        self.chrome.setWantsLayer_(True)
        view.addSubview_(self.chrome)
        layer = self.chrome.layer()
        self.tint = _bare(Q.CAGradientLayer.layer())
        self.tint.setStartPoint_((0.5, 1))
        self.tint.setEndPoint_((0.5, 0))
        self.tint.setColors_([rgb(0, 0, 0), rgb(0, 0, 0), rgb(0.047, 0.047, 0.07, 0.5), rgb(0.047, 0.047, 0.07, 0.3)])
        self.tint_mask = _bare(Q.CAShapeLayer.layer())
        self.tint.setMask_(self.tint_mask)
        layer.addSublayer_(self.tint)
        self.rim = _bare(Q.CAShapeLayer.layer())
        self.rim.setFillColor_(CLEAR)
        self.rim.setStrokeColor_(rgb(1, 1, 1, 0.16))
        self.rim.setLineWidth_(1)
        self.rim_fade = _bare(Q.CAGradientLayer.layer())
        self.rim_fade.setStartPoint_((0.5, 1))
        self.rim_fade.setEndPoint_((0.5, 0))
        self.rim_fade.setColors_([CLEAR, CLEAR, WHITE])
        self.rim.setMask_(self.rim_fade)
        layer.addSublayer_(self.rim)
        self.corona = _bare(Q.CAGradientLayer.layer())
        self.corona.setStartPoint_((0.5, 1))
        self.corona.setEndPoint_((0.5, 0))
        self.corona.setColors_([CLEAR, CLEAR, rgb(0.49, 0.55, 1, 0.9), rgb(0.66, 0.45, 1, 0.85), rgb(0.37, 0.83, 0.9, 0.7)])
        self.corona_mask = _bare(Q.CAShapeLayer.layer())
        self.corona_mask.setFillColor_(CLEAR)
        self.corona_mask.setStrokeColor_(WHITE)
        self.corona_mask.setLineWidth_(1.6)
        self.corona.setMask_(self.corona_mask)
        self.corona.setOpacity_(0)
        layer.addSublayer_(self.corona)
        self.glow = 0.0

    def layout(self, bounds, scale):
        for item in (self.effect, self.chrome):
            item.setFrame_(bounds)
        for item in (self.aura, self.effect_mask, self.tint, self.tint_mask, self.rim, self.rim_fade,
                     self.corona, self.corona_mask):
            item.setFrame_(item.superlayer().bounds() if item.superlayer() is not None else bounds)
            item.setContentsScale_(scale)

    def update(self, path, canvas_height, island_height, band):
        """Reshape every layer to the island's current path; colour stops follow its animated height."""
        h = max(1.0, canvas_height)
        bottom = min(1.0, max(island_height, 1) / h)
        band_end = min(bottom, (band + 2) / h)
        fade_end = min(1.0, max(band_end + 0.001, (band + BAND_FADE) / h))
        self.aura.setShadowPath_(path)
        self.effect_mask.setPath_(path)
        self.tint_mask.setPath_(path)
        self.rim.setPath_(path)
        self.corona_mask.setPath_(path)
        self.tint.setLocations_([0, band_end, fade_end, max(fade_end + 0.001, bottom)])
        self.rim_fade.setLocations_([0, min(1.0, (band + 10) / h), min(1.0, (band + 86) / h)])
        self.corona.setLocations_([0, min(1.0, (band - 4) / h), min(1.0, (band + 80) / h),
                                   max(min(1.0, (band + 81) / h), bottom * 0.62),
                                   max(min(1.0, (band + 82) / h), bottom)])

    def set_glow(self, value):
        self.glow = value
        self.corona.setOpacity_(value)
        self.aura.setShadowOpacity_(0.32 * value)


class DiamondRing:
    """Luna's mark: a ring with one bright bead. The bead carries listening, thinking and speaking state."""

    RADIUS = 5.0

    def __init__(self, parent, scale):
        self.layer = _bare(Q.CALayer.layer())
        self.layer.setBounds_(NSMakeRect(0, 0, 16, 16))
        self.ring = _bare(Q.CAShapeLayer.layer())
        self.ring.setFrame_(self.layer.bounds())
        ring = Q.CGPathCreateMutable()
        Q.CGPathAddEllipseInRect(ring, None, NSMakeRect(8 - self.RADIUS, 8 - self.RADIUS, 2 * self.RADIUS, 2 * self.RADIUS))
        self.ring.setPath_(ring)
        self.ring.setFillColor_(CLEAR)
        self.ring.setLineWidth_(1.35)
        self.bead = _bare(Q.CAShapeLayer.layer())
        self.bead.setFrame_(self.layer.bounds())
        self.bead.setShadowOffset_((0, 0))
        self.bead.setShadowRadius_(3)
        for item in (self.ring, self.bead):
            item.setContentsScale_(scale)
            self.layer.addSublayer_(item)
        parent.addSublayer_(self.layer)
        self.state = None
        self.set_state("ready")

    def set_state(self, state, level=0.0, phase=0.0):
        """`phase` (0–1) moves the bead clockwise around the ring while thinking."""
        self.state = state
        angle = math.radians(45 - 360 * phase) if state == "thinking" else math.radians(45)
        size = 1.75 + (1.5 * level if state in ("listening", "speaking") else 0)
        ring, bead, glow = rgb(1, 1, 1, 0.55), WHITE, 0.0
        if state == "listening":
            glow = 0.5 + 0.5 * level
        elif state == "thinking":
            ring = rgb(1, 1, 1, 0.28)
        elif state == "speaking":
            ring, bead, glow = VIOLET, LILAC, 0.9
        elif state == "attention":
            ring, bead = rgb(1, 1, 1, 0.3), AMBER
        x, y = 8 + self.RADIUS * math.cos(angle), 8 + self.RADIUS * math.sin(angle)
        path = Q.CGPathCreateMutable()
        Q.CGPathAddEllipseInRect(path, None, NSMakeRect(x - size, y - size, 2 * size, 2 * size))
        self.ring.setStrokeColor_(ring)
        self.bead.setPath_(path)
        self.bead.setFillColor_(bead)
        self.bead.setShadowColor_(VIOLET if state == "speaking" else WHITE)
        self.bead.setShadowOpacity_(glow)


class Wave:
    """A soft line in the right wing that follows the five audio levels; it replaces the old bars."""

    WIDTH, HEIGHT = 40.0, 14.0

    def __init__(self, parent, scale):
        self.layer = _bare(Q.CAShapeLayer.layer())
        self.layer.setBounds_(NSMakeRect(0, 0, self.WIDTH, self.HEIGHT))
        self.layer.setFillColor_(CLEAR)
        self.layer.setLineWidth_(1.6)
        self.layer.setLineCap_(Q.kCALineCapRound)
        self.layer.setLineJoin_(Q.kCALineJoinRound)
        self.layer.setContentsScale_(scale)
        parent.addSublayer_(self.layer)

    def update(self, levels, speaking, phase):
        path = Q.CGPathCreateMutable()
        mid = self.HEIGHT / 2
        steps = 40
        for i in range(steps + 1):
            x = self.WIDTH * i / steps
            position = (len(levels) - 1) * i / steps
            low = int(position)
            high = min(low + 1, len(levels) - 1)
            level = levels[low] + (levels[high] - levels[low]) * (position - low)
            # Taper the ends so the line grows out of the wing instead of starting mid-air.
            taper = math.sin(math.pi * i / steps)
            y = mid + (0.6 + 5.6 * level) * taper * math.sin(i * 0.55 + phase * 2 * math.pi)
            if i == 0:
                Q.CGPathMoveToPoint(path, None, x, y)
            else:
                Q.CGPathAddLineToPoint(path, None, x, y)
        self.layer.setPath_(path)
        self.layer.setStrokeColor_(VIOLET if speaking else WHITE)
