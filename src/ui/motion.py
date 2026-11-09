"""A retargetable damped spring shared by the island and all of its contents."""

import math


class Spring:
    def __init__(self, value):
        self.value = self.target = tuple(value)
        self.velocity = (0.0,) * len(value)
        self.damping = 0.82

    def move_to(self, target, *, immediate=False, closing=False):
        self.target = tuple(target)
        self.damping = 1.0 if closing else 0.82
        if immediate:
            self.value = self.target
            self.velocity = (0.0,) * len(self.target)

    @property
    def moving(self):
        return any(abs(a - b) > 0.025 or abs(v) > 0.05
                   for a, b, v in zip(self.value, self.target, self.velocity))

    def advance(self, elapsed):
        elapsed = max(0, elapsed)
        if not self.moving:
            self.value = self.target
            self.velocity = (0.0,) * len(self.target)
            return self.value
        omega = 2 * math.pi / 0.42
        zeta = self.damping
        decay = math.exp(-zeta * omega * max(0, elapsed))
        values, velocities = [], []
        for current, target, velocity in zip(self.value, self.target, self.velocity):
            offset = current - target
            if zeta == 1:
                term = velocity + omega * offset
                next_offset = decay * (offset + term * elapsed)
                next_velocity = decay * (velocity - omega * term * elapsed)
            else:
                frequency = omega * math.sqrt(1 - zeta * zeta)
                sine, cosine = math.sin(frequency * elapsed), math.cos(frequency * elapsed)
                next_offset = decay * (offset * cosine + (velocity + zeta * omega * offset) / frequency * sine)
                next_velocity = decay * (velocity * cosine - (zeta * omega * velocity + omega * omega * offset) / frequency * sine)
            values.append(target + next_offset)
            velocities.append(next_velocity)
        self.value, self.velocity = tuple(values), tuple(velocities)
        if not self.moving:
            self.value = self.target
            self.velocity = (0.0,) * len(self.target)
        return self.value
