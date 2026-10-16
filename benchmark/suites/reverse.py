"""A reverse foreign key between two views: is there a fence that always pays?

`Person.objects.filter(personaddress__address_id__in=...)`, where
`PersonAddress.person` is an OverlayForeignKey and both models are overlay
views, joins one appendrel to another. The forward-FK rewrite cannot replace
that join, because a person with two matching rows has to come back twice, and
the m2m fence only knows link tables -- so today it compiles as the plain join,
the shape the rest of the library exists to avoid.

The m2m fence's conjunct carries over unchanged: `pk = ANY (ARRAY(SELECT
person_id FROM related WHERE ...))` is implied by the join, so ANDing it on
cannot change the rows. Tried by hand first, it was several times faster on a
narrow scope and several times *slower* on a statement that already carried
other array fences, where the planner's default guess for each array's size
compounded until tens of thousands of rows were estimated as a handful and a
nested loop compared a billion pairs. That is why it is not on by default, and
this suite is what any rule for turning it on has to argue against.

Three arms, all through the ORM, all returning the same rows:

- **join**: what the library compiles today.
- **join + fence**: the same join with the ARRAY conjunct written by hand.
- **semi-join**: the fence with no join at all. Only valid where duplicates do
  not matter -- a scope, a count of distinct people -- which is every operation
  here, so it is measured as the lever a caller has when they know that.

Two sections, because the candidate rule is about what else is in the
statement: the reverse condition on its own, and the same condition ANDed with
an OR of two other array fences, the shape that went wrong.
"""

from django.db.models import Q

from benchmark import harness


NAME = "reverse"
TITLE = "A reverse foreign key between two views: join, fence, or semi-join"

# Narrow to broad by how many link rows match. `city` carries 1,000 distinct
# values, so each step is roughly ten times the last; the last is every link,
# expressed through the link's own column so it stays a reverse-FK condition.
CITY_SCOPES = (
    ("1 city", 1),
    ("10 cities", 10),
    ("100 cities", 100),
)

COLUMNS = ("join", "join + fence", "fence gain", "semi-join", "semi gain", "plain", "rows")


def _reverse_condition(address_ids):
    """The reverse-FK condition, on the link view's own column."""
    if address_ids is None:
        return {"benchpersonaddress__role__in": ["home", "work", "billing"]}
    return {"benchpersonaddress__address_id__in": address_ids}


def _plain_condition(address_ids):
    if address_ids is None:
        return {"plainpersonaddress__role__in": ["home", "work", "billing"]}
    return {"plainpersonaddress__address_id__in": address_ids}


def _links(address_ids):
    from tests.testapp.models import BenchPersonAddress

    links = BenchPersonAddress.objects.all()
    if address_ids is None:
        links = links.filter(role__in=["home", "work", "billing"])
    else:
        links = links.filter(address_id__in=address_ids)
    return links.values("person_id")


def _other_fences():
    """Two more array fences, OR'd: the shape that made the fence lose."""
    from tests.testapp.models import BenchPersonEmail, BenchPersonPhone

    phones = BenchPersonPhone.objects.filter(phone__kind="home").values("person_id")
    emails = BenchPersonEmail.objects.filter(email__kind="work").values("person_id")
    return Q(pk__overlay_selective_in=phones) | Q(pk__overlay_selective_in=emails)


def _plain_others():
    from tests.testapp.models import PlainPersonEmail, PlainPersonPhone

    phones = PlainPersonPhone.objects.filter(phone__kind="home").values("person_id")
    emails = PlainPersonEmail.objects.filter(email__kind="work").values("person_id")
    return Q(pk__in=phones) | Q(pk__in=emails)


def scope(arm, address_ids, with_others):
    """The people the condition matches, as a distinct count -- a saved search."""
    from tests.testapp.models import BenchPerson, PlainPerson

    if arm == "plain":
        people = PlainPerson.objects.filter(**_plain_condition(address_ids))
        if with_others:
            people = people.filter(_plain_others())
    else:
        people = BenchPerson.objects.all()
        if arm in ("join", "join + fence"):
            people = people.filter(**_reverse_condition(address_ids))
        if arm in ("join + fence", "semi-join"):
            people = people.filter(pk__overlay_selective_in=_links(address_ids))
        if with_others:
            people = people.filter(_other_fences())
    return people.values("pk").distinct().count()


def row(ctx, section, label, address_ids, with_others):
    cells, values = {}, {}
    for arm in ("join", "join + fence", "semi-join", "plain"):
        cells[arm], values[arm] = ctx.measure(lambda arm=arm: scope(arm, address_ids, with_others))

    # Every arm answers the same question, so a difference is the finding.
    for arm in ("join + fence", "semi-join", "plain"):
        ctx.compare(f"{label} ({arm} vs join)", values["join"], values[arm], f"{arm} changed the result")

    section.add(
        label,
        cells,
        **{
            "fence gain": harness.gain(cells["join"], cells["join + fence"]),
            "semi gain": harness.gain(cells["join"], cells["semi-join"]),
        },
        rows="capped" if values["join"] is None else f"{values['join']:,}",
    )


def run(ctx):
    from tests.testapp.models import BenchAddress

    # Resolved once, outside the timings: a list of ids is a constant the
    # planner can estimate, as a saved search's condition values are, so the
    # only array subqueries in the statement are the ones being compared.
    scopes = [
        (
            label,
            list(
                BenchAddress.objects.filter(city__in=[f"city{n}" for n in range(cities)]).values_list("pk", flat=True)
            ),
        )
        for label, cities in CITY_SCOPES
    ]
    scopes.append(("every link", None))

    for attempt in range(1, ctx.passes + 1):
        suffix = f" (pass {attempt})" if ctx.passes > 1 else ""

        section = harness.Section(
            f"The reverse condition alone{suffix}",
            COLUMNS,
            note="each gain is that column against the plain join",
        )
        for label, address_ids in scopes:
            row(ctx, section, label, address_ids, with_others=False)
        yield section

        section = harness.Section(
            f"The same condition beside two other array fences{suffix}",
            COLUMNS,
            note="each array's size is a planner guess; the guesses compound",
        )
        for label, address_ids in scopes:
            row(ctx, section, label, address_ids, with_others=True)
        yield section
