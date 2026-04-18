def color_from_state(state: str = "normal", bg_color: tuple[int, int, int] = (37, 37, 37)) -> tuple[int, int, int]:
    r, g, b = bg_color
    factor = (-1.0) if r < 145 or g < 145 or b < 145 else (1.0)
    if state == "pressed":
        return (
            max(0, min(255, int(r - (r * 0.3) * factor))),
            max(0, min(255, int(g - (g * 0.3) * factor))),
            max(0, min(255, int(b - (b * 0.3) * factor))),
        )
    if state == "hovered":
        return (
            max(0, min(255, int(r - (r * 0.15) * factor))),
            max(0, min(255, int(g - (g * 0.15) * factor))),
            max(0, min(255, int(b - (b * 0.15) * factor))),
        )
    if state == "inactive":
        return (
            max(0, min(255, int(0.3 * r))),
            max(0, min(255, int(0.3 * g))),
            max(0, min(255, int(0.3 * b))),
        )
    if state == "separator":
        return (
            max(0, min(255, int(r - (r * 0.6) * factor))),
            max(0, min(255, int(g - (g * 0.6) * factor))),
            max(0, min(255, int(b - (b * 0.6) * factor))),
        )
    return bg_color
