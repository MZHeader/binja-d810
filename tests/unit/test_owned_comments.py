import pytest
from binja_d810.consteval.comments import remove_line


@pytest.mark.parametrize("before", ("", "user", "user\n", "user\n\n", "first\nsecond"))
def test_removing_the_unchanged_annotation_restores_exact_user_text(before):
    appended = before + ("\n" if before else "") + "owned"
    assert remove_line(appended, "owned") == before


def test_user_edits_and_other_annotations_survive():
    assert remove_line("user\nowned edited", "owned") == "user\nowned edited"
    assert remove_line("user\nowned\nother\n", "owned") == "user\nother\n"
    assert remove_line("owned\nuser", "owned") == "user"
