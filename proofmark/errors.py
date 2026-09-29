"""Errors whose message is written for the person using the app."""


class UserInputError(ValueError):
    """The request cannot be processed as sent; the message says how to fix it."""
