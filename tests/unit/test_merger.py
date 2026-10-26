from datetime import date

from customer_pipeline.merger import CustomerMerger, UnionFind
from tests.conftest import make_record


def test_union_find_groups_chains():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")
    assert uf.find("d") != uf.find("a")


def test_records_without_matches_stay_separate():
    a = make_record(source_id="1", email="a@example.com", phone="+919000020001")
    b = make_record(source_id="2", email="b@example.com", phone="+919000020002")
    assert len(CustomerMerger().merge([a, b])) == 2


def test_same_email_merges():
    a = make_record(source="crm", source_id="C1", email="same@example.com", phone=None)
    b = make_record(source="billing", source_id="B1", email="same@example.com", phone=None)
    golden = CustomerMerger().merge([a, b])
    assert len(golden) == 1
    assert {m.key for m in golden[0].members} == {"crm:C1", "billing:B1"}


def test_same_phone_merges_even_with_different_email():
    a = make_record(source="crm", source_id="C1", email="home@example.com", phone="+919000020009")
    b = make_record(source="billing", source_id="B1", email="work@example.com", phone="+919000020009")
    golden = CustomerMerger().merge([a, b])
    assert len(golden) == 1
    assert golden[0].email == "home@example.com"  # CRM wins for email


def test_matching_is_transitive():
    # A~B by email, B~C by phone, A and C share nothing directly.
    a = make_record(source_id="A", email="one@example.com", phone="+919000020001")
    b = make_record(source_id="B", email="one@example.com", phone="+919000020002")
    c = make_record(source_id="C", email="two@example.com", phone="+919000020002")
    assert len(CustomerMerger().merge([a, b, c])) == 1


def test_survivorship_crm_wins_names_billing_wins_plan():
    crm = make_record(source="crm", source_id="C1", first_name="Priya", city="Bengaluru",
                      plan="Basic", email="p@example.com")
    billing = make_record(source="billing", source_id="B1", first_name="P.", city="Bangalore",
                          plan="Pro", email="p@example.com")
    golden = CustomerMerger().merge([billing, crm])[0]
    assert golden.first_name == "Priya"
    assert golden.city == "Bengaluru"
    assert golden.plan == "Pro"


def test_survivorship_falls_back_when_preferred_source_is_empty():
    crm = make_record(source="crm", source_id="C1", email="m@example.com", phone=None)
    billing = make_record(source="billing", source_id="B1", email="m@example.com", phone="+919000030001")
    golden = CustomerMerger().merge([crm, billing])[0]
    assert golden.phone == "+919000030001"


def test_customer_since_is_earliest_date():
    a = make_record(source_id="1", email="d@example.com", signup_date=date(2024, 3, 20))
    b = make_record(source_id="2", email="d@example.com", signup_date=date(2024, 3, 15))
    assert CustomerMerger().merge([a, b])[0].customer_since == date(2024, 3, 15)


def test_merge_is_deterministic_regardless_of_input_order():
    a = make_record(source="crm", source_id="C2", email="z@example.com", first_name="Second")
    b = make_record(source="crm", source_id="C1", email="z@example.com", first_name="First")
    g1 = CustomerMerger().merge([a, b])[0]
    g2 = CustomerMerger().merge([b, a])[0]
    assert g1.first_name == g2.first_name == "First"
