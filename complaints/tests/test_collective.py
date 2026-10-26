"""A Sarpanch's seal means the Sarpanch complains; many signatures without one, many people."""

from complaints.services import intake


def guess(text, office, latin=""):
    return intake.guess_fields(text, office, latin)


def test_a_sarpanch_seal_makes_the_sarpanch_the_complainant(office):
    text = "टावर को हटाया जाये\nसभी नौरंग वासी"
    seal = "Sarpanch\nGram Panchayat, Naurang\nBlock Odhan (Sirsa)"
    assert guess(text, office, seal).name == "Sarpanch, Gram Panchayat Naurang"


def test_a_hindi_sarpanch_stamp_is_enough(office):
    assert guess("सरपंच ग्राम पंचायत\nसभी ग्रामवासी", office).name == "Sarpanch"


def test_many_villagers_without_a_seal_are_multiple_complainants(office):
    assert guess("बिजली की समस्या है\nधन्यवाद सभी नौरंग वासी", office).name == "Multiple complainants"


def test_an_ordinary_letter_is_not_collective(office):
    text = "विषय :- सड़क।\nप्रार्थीगण\nरामलाल पुत्र मोहन सिंह निवासी गांव तोहाना"
    assert guess(text, office).name == "रामलाल"


def test_a_letter_that_only_mentions_the_sarpanch_is_not_a_sarpanch_letter(office):
    text = "मैंने गांव के सरपंच को कई बार कहा है\nप्रार्थी\nसुखपाल सिह पुत्र हरनेक सिह निवासी गांव फुल्लो"
    assert guess(text, office).name != "Sarpanch"
