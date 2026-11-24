"""One class per page. Page is what they share."""


class Page:
    name = title = ""
    drives = False             # a keyboard profile drives on this page
    chrome = True              # drawn between the tabs and the footer
    footer = 48                # height of the footer, in design units

    def __init__(self, app):
        self.app, self.ui, self.link = app, app.ui, app.link

    @property
    def status(self):
        return self.link.status

    def opened(self):
        pass

    def back(self):
        """Leave whatever is open inside the page. False if nothing was."""
        return False

    def key(self, key, repeat):
        """True if the page used the key itself."""
        return False

    def captures_keys(self):
        """Is the page using the keyboard for something of its own right now?"""
        return False

    def draw(self, area):
        raise NotImplementedError

    def draw_footer(self, area):
        self.app.draw_footer(area)
