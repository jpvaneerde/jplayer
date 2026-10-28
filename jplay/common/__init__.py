"""Code the player, the display and the hub share."""


def no_stale_pages(app) -> None:
    """Browsers (above all the kiosk's Chromium) must ask again for pages, styles and scripts every time instead
    of reusing a stored copy: after an update the screens show the new version at once. Answers that set their
    own caching (covers, art) keep it; asking again is cheap (the files' ETags make it a short "not modified")."""

    @app.middleware("http")
    async def _no_stale(request, call_next):
        response = await call_next(request)
        if request.method == "GET" and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
        return response
