"""The public JSON shapes.

These are the single description of what the API returns: FastAPI renders them
into the OpenAPI document that `/docs` displays, and the store writes the same
models to disk as its cached state. One definition, so the published schema and
the persisted state cannot drift apart.

They are deliberately not the same types as the `upstream` dataclasses. Those
mirror what Convex happens to return today; these are a contract we control.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .htmltext import html_to_text
from .upstream import Dataset, NavetCompany, NavetEvent, NavetJobListing, NavetOrganizer


class Organizer(BaseModel):
    """A person responsible for an event."""

    id: str = Field(description="Upstream id of the organizer record.")
    name: str = Field(description="Full name.")
    role: str = Field(description="Either `hovedansvarlig` or `medhjelper`.", examples=["hovedansvarlig"])
    image_url: str | None = Field(default=None, description="Profile picture, when the organizer has one.")
    email: str | None = Field(
        default=None,
        description=(
            "Contact address. Always `null` unless the deployment sets "
            "`INCLUDE_ORGANIZER_EMAILS=true`, because these are personal "
            "addresses of Navet volunteers rather than role accounts."
        ),
    )

    @classmethod
    def from_domain(cls, organizer: NavetOrganizer) -> Organizer:
        return cls(
            id=organizer.id,
            name=organizer.name,
            role=organizer.role,
            image_url=organizer.image_url,
            email=organizer.email,
        )

    def to_domain(self) -> NavetOrganizer:
        return NavetOrganizer(id=self.id, name=self.name, role=self.role, image_url=self.image_url, email=self.email)


class Company(BaseModel):
    """A company in Navet's register."""

    id: str = Field(description="Upstream company id, stable across refreshes.")
    name: str = Field(description="Company name.", examples=["Bekk"])
    description: str = Field(description="Plain-text description, converted from the upstream HTML.")
    description_html: str = Field(description="The description as upstream stores it.")
    org_number: int | None = Field(default=None, description="Norwegian organisation number.")
    main_sponsor: bool = Field(description="Whether this company is Navet's current main sponsor.")
    image_url: str | None = Field(
        default=None,
        description=(
            "Logo URL, present only when the logo is in a format consumers can "
            "render (see `IMAGE_TYPES`). Several companies use SVG, which is "
            "reported as `null` here rather than as a link clients break on."
        ),
    )
    image_type: str | None = Field(default=None, description="Media type of `image_url`.", examples=["image/png"])

    @classmethod
    def from_domain(cls, company: NavetCompany) -> Company:
        return cls(
            id=company.id,
            name=company.name,
            description=html_to_text(company.description_html),
            description_html=company.description_html,
            org_number=company.org_number,
            main_sponsor=company.main_sponsor,
            image_url=company.image_url,
            image_type=company.image_type,
        )

    def to_domain(self) -> NavetCompany:
        return NavetCompany(
            id=self.id,
            name=self.name,
            description_html=self.description_html,
            org_number=self.org_number,
            main_sponsor=self.main_sponsor,
            # The logo id is an upstream-cache detail, not part of the contract.
            # Dropping it on reload just means the next refresh re-resolves.
            logo_id=None,
            image_url=self.image_url,
            image_type=self.image_type,
        )


class Event(BaseModel):
    """A published Navet event."""

    id: str = Field(description="Upstream event id. The calendar UID is `<id>@ifinavet.no`.")
    title: str
    slug: str | None = Field(default=None, description="URL slug, when the event has one.")
    url: str = Field(description="Permalink on ifinavet.no.")
    external_url: str | None = Field(default=None, description="Set when registration happens off-site.")
    start: datetime = Field(description="Event start, UTC. Upstream stores no end time.")
    registration_opens: datetime | None = Field(default=None, description="When registration opens, UTC.")
    has_registration_form: bool = Field(description="Whether registration goes through Navet's own form.")
    teaser: str
    description: str = Field(description="Plain-text body, converted from the upstream HTML.")
    description_html: str = Field(description="The body as upstream stores it.")
    location: str
    food: str
    language: str
    age_restriction: str
    participation_limit: int | None = Field(default=None, description="Seat cap, when one is set.")
    company_id: str = Field(description="Id of the hosting company; look it up under `/api/companies`.")
    company: str = Field(description="Hosting company name.")
    organizers: list[Organizer] = Field(
        default_factory=list,
        description="Empty when `FETCH_ORGANIZERS` is off or the lookup ceiling was reached.",
    )
    image_url: str | None = Field(default=None, description="Hosting company's logo, when renderable.")
    image_type: str | None = Field(default=None, description="Media type of `image_url`.")
    created: datetime = Field(description="When the event was created upstream, UTC.")

    @classmethod
    def from_domain(cls, event: NavetEvent) -> Event:
        return cls(
            id=event.uid,
            title=event.title,
            slug=event.slug,
            url=event.url,
            external_url=event.external_url,
            start=event.start,
            registration_opens=event.registration_opens,
            has_registration_form=event.has_registration_form,
            teaser=event.teaser,
            description=html_to_text(event.description_html),
            description_html=event.description_html,
            location=event.location,
            food=event.food,
            language=event.language,
            age_restriction=event.age_restriction,
            participation_limit=event.participation_limit,
            company_id=event.company_id,
            company=event.company,
            organizers=[Organizer.from_domain(o) for o in event.organizers],
            image_url=event.image_url,
            image_type=event.image_type,
            created=event.created,
        )

    def to_domain(self) -> NavetEvent:
        return NavetEvent(
            uid=self.id,
            title=self.title,
            start=self.start,
            teaser=self.teaser,
            description_html=self.description_html,
            location=self.location,
            company=self.company,
            company_id=self.company_id,
            food=self.food,
            language=self.language,
            age_restriction=self.age_restriction,
            url=self.url,
            external_url=self.external_url,
            created=self.created,
            participation_limit=self.participation_limit,
            registration_opens=self.registration_opens,
            slug=self.slug,
            has_registration_form=self.has_registration_form,
            image_url=self.image_url,
            image_type=self.image_type,
            organizers=tuple(o.to_domain() for o in self.organizers),
        )


class JobListing(BaseModel):
    """A job advertised through ifinavet.no."""

    id: str = Field(description="Upstream listing id. The calendar UID is `<id>-deadline@ifinavet.no`.")
    title: str
    type: str = Field(description="Listing category.", examples=["Sommerjobb", "Internship", "Fulltid", "Deltid"])
    url: str = Field(description="Permalink on ifinavet.no.")
    application_url: str | None = Field(default=None, description="Where to apply, when upstream provides it.")
    deadline: datetime = Field(description="Application deadline, UTC.")
    teaser: str
    description: str = Field(description="Plain-text body, converted from the upstream HTML.")
    description_html: str = Field(description="The body as upstream stores it.")
    company_id: str
    company: str
    image_url: str | None = Field(default=None, description="Company logo, when renderable.")
    image_type: str | None = Field(default=None, description="Media type of `image_url`.")
    created: datetime = Field(description="When the listing was created upstream, UTC.")

    @classmethod
    def from_domain(cls, listing: NavetJobListing) -> JobListing:
        return cls(
            id=listing.uid,
            title=listing.title,
            type=listing.kind,
            url=listing.url,
            application_url=listing.application_url,
            deadline=listing.deadline,
            teaser=listing.teaser,
            description=html_to_text(listing.description_html),
            description_html=listing.description_html,
            company_id=listing.company_id,
            company=listing.company,
            image_url=listing.image_url,
            image_type=listing.image_type,
            created=listing.created,
        )

    def to_domain(self) -> NavetJobListing:
        return NavetJobListing(
            uid=self.id,
            title=self.title,
            kind=self.type,
            teaser=self.teaser,
            description_html=self.description_html,
            application_url=self.application_url,
            deadline=self.deadline,
            company_id=self.company_id,
            company=self.company,
            url=self.url,
            created=self.created,
            image_url=self.image_url,
            image_type=self.image_type,
        )


class DatasetDocument(BaseModel):
    """The whole cached dataset. Also the on-disk state format."""

    version: int = Field(default=1, description="Bumped when the on-disk shape changes incompatibly.")
    generated_at: datetime
    events: list[Event] = Field(default_factory=list)
    companies: list[Company] = Field(default_factory=list)
    job_listings: list[JobListing] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, dataset: Dataset, generated_at: datetime) -> DatasetDocument:
        return cls(
            generated_at=generated_at,
            events=[Event.from_domain(e) for e in dataset.events],
            companies=[Company.from_domain(c) for c in dataset.companies],
            job_listings=[JobListing.from_domain(j) for j in dataset.job_listings],
        )

    def to_domain(self) -> Dataset:
        return Dataset(
            events=[e.to_domain() for e in self.events],
            companies=[c.to_domain() for c in self.companies],
            job_listings=[j.to_domain() for j in self.job_listings],
        )


class Page(BaseModel):
    """A slice of a collection. Every list endpoint returns this envelope.

    Subclassed per item type rather than carrying a union: a union would let
    Pydantic coerce a company into an event when their fields happen to
    overlap, and would document every list endpoint as returning any of the
    three.
    """

    total: int = Field(description="Matching items before paging.")
    limit: int = Field(description="Page size actually applied, after `MAX_PAGE_SIZE` capping.")
    offset: int


class EventPage(Page):
    items: list[Event]


class CompanyPage(Page):
    items: list[Company]


class JobListingPage(Page):
    items: list[JobListing]


class FeedInfo(BaseModel):
    path: str
    events: int
    etag: str


class Status(BaseModel):
    """What `/readyz` and `/api/status` report."""

    ready: bool = Field(description="A feed exists and upstream data is not dangerously old.")
    stale: bool = Field(description="Whether the last successful refresh is older than `STALE_AFTER_SECONDS`.")
    events: int
    companies: int
    job_listings: int
    generated_at: datetime | None
    last_success: datetime | None
    last_attempt: datetime | None
    last_error: str | None
    feeds: list[FeedInfo]
    now: datetime
