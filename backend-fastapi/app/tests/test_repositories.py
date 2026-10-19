import threading
from dataclasses import replace
from datetime import date

from app.models.contacts import Contact, ContactPermissions
from app.models.enums import Role
from app.repositories import contact_repository, user_repository
from app.repositories.contact_repository import (
    ContactRepository,
    InMemoryContactRepository,
)
from app.repositories.user_repository import InMemoryUserRepository, UserRepository
from app.models.users import (
    NO_CAREGIVER_PERMISSIONS,
    AdultLink,
    CaregiverPermissions,
    UserProfile,
    normalize_email,
)
from app.repositories import device_repository
from app.repositories.device_repository import (
    DeviceRepository,
    InMemoryDeviceRepository,
)


ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"


def _contact(contact_id: str, first_name: str = "Ana") -> Contact:
    return Contact(
        id=contact_id,
        first_name=first_name,
        last_name="Pérez",
        relationship="hija",
        phone="+573001234567",
        permissions=ContactPermissions(
            calls=True,
            video_calls=True,
            messages=False,
            help_alerts=True,
        ),
    )


def test_contact_repository_add_get_and_list():
    repo: ContactRepository = InMemoryContactRepository()
    first = _contact("contact-1", "Ana")
    second = _contact("contact-2", "Luis")

    saved = repo.add(ADULT_UID, first)
    repo.add(ADULT_UID, second)

    assert saved == first
    assert saved.email is None
    assert saved.photo_url is None
    assert saved.is_favorite is False
    assert saved.favorite_order == 0
    assert repo.get(ADULT_UID, "contact-1") == first
    assert repo.get(ADULT_UID, "contact-2") == second
    assert repo.get(ADULT_UID, "missing") is None
    assert repo.list_for_adult(ADULT_UID) == [first, second]

    returned = repo.get(ADULT_UID, "contact-1")
    returned.first_name = "Cambiado"
    returned.permissions.calls = False
    assert repo.get(ADULT_UID, "contact-1") == first


def test_contact_repository_isolates_adults():
    repo = InMemoryContactRepository()
    repo.add(ADULT_UID, _contact("contact-1"))

    assert repo.get(OTHER_ADULT_UID, "contact-1") is None
    assert repo.list_for_adult(OTHER_ADULT_UID) == []

    repo.add(OTHER_ADULT_UID, _contact("contact-2", "Luis"))

    assert repo.get(ADULT_UID, "contact-2") is None
    assert [contact.id for contact in repo.list_for_adult(ADULT_UID)] == ["contact-1"]
    assert [contact.id for contact in repo.list_for_adult(OTHER_ADULT_UID)] == [
        "contact-2"
    ]


def test_user_repository_links_adults():
    repo: UserRepository = InMemoryUserRepository()

    assert repo.is_linked("family-uid", ADULT_UID) is False
    assert repo.adults_of("family-uid") == []

    repo.link_adult("family-uid", ADULT_UID, Role.FAMILIAR)
    repo.link_adult("family-uid", OTHER_ADULT_UID, Role.FAMILIAR)
    repo.link_adult("caregiver-uid", ADULT_UID, Role.CUIDADOR)

    assert repo.is_linked("family-uid", ADULT_UID) is True
    assert repo.is_linked("family-uid", OTHER_ADULT_UID) is True
    assert repo.is_linked("caregiver-uid", ADULT_UID) is True
    assert repo.is_linked("caregiver-uid", OTHER_ADULT_UID) is False
    assert repo.is_linked(ADULT_UID, "family-uid") is False
    assert repo.adults_of("family-uid") == [ADULT_UID, OTHER_ADULT_UID]
    assert repo.adults_of("caregiver-uid") == [ADULT_UID]
    assert repo.adults_of("stranger-uid") == []

    repo.link_adult("family-uid", ADULT_UID, Role.FAMILIAR)
    assert repo.adults_of("family-uid") == [ADULT_UID, OTHER_ADULT_UID]


def test_repository_providers_return_shared_instance():
    contacts = contact_repository.get_contact_repository()
    users = user_repository.get_user_repository()

    assert isinstance(contacts, InMemoryContactRepository)
    assert isinstance(users, InMemoryUserRepository)
    assert contact_repository.get_contact_repository() is contacts
    assert user_repository.get_user_repository() is users
    assert contacts is contact_repository._default_contact_repository
    assert users is user_repository._default_user_repository


# --- Ronda 2 (F1a): perfiles, correos verificados, vínculos con permisos, contactos y dispositivos ---

FAMILY_UID = "family-uid"
SECOND_FAMILY_UID = "second-family-uid"
CAREGIVER_UID = "caregiver-uid"
SECOND_CAREGIVER_UID = "second-caregiver-uid"
NO_PERMISSIONS = CaregiverPermissions(
    manage_contacts=False, manage_reminders=False, receive_alerts=False
)


def _permissions(
    manage_contacts: bool = True,
    manage_reminders: bool = False,
    receive_alerts: bool = True,
) -> CaregiverPermissions:
    return CaregiverPermissions(
        manage_contacts=manage_contacts,
        manage_reminders=manage_reminders,
        receive_alerts=receive_alerts,
    )


def _profile(
    uid: str = FAMILY_UID,
    email: str = "  Ana.Perez@Example.COM ",
    role: Role = Role.FAMILIAR,
    **extra,
) -> UserProfile:
    return UserProfile(
        uid=uid,
        first_name="Ana",
        last_name="Pérez",
        email=email,
        role=role,
        **extra,
    )


def test_normalize_email_strips_and_lowercases():
    assert normalize_email("  Ana.Perez@Example.COM \n") == "ana.perez@example.com"
    assert normalize_email("ana@example.com") == "ana@example.com"
    assert normalize_email("   ") == ""


def test_user_profile_defaults_and_full_name():
    profile = UserProfile(
        uid="uid-1",
        first_name="Rosa",
        last_name="Gómez",
        email="rosa@example.com",
        role=Role.ADULTO_MAYOR,
    )

    assert profile.phone is None
    assert profile.birth_date is None
    assert profile.address is None
    assert profile.photo_url is None
    assert profile.email_verified is False
    assert profile.full_name == "Rosa Gómez"
    assert replace(profile, last_name="").full_name == "Rosa"
    assert replace(profile, first_name="", last_name="").full_name == ""


def test_user_repository_add_and_get_profile_returns_copies():
    repo: UserRepository = InMemoryUserRepository()
    original = _profile(
        phone="+573001234567",
        birth_date=date(1950, 5, 1),
        address="Calle 1 # 2-3",
        photo_url="https://example.com/ana.png",
    )

    saved = repo.add_profile(original)

    assert saved == replace(original, email="ana.perez@example.com")
    assert repo.get_profile(FAMILY_UID) == saved
    assert original.email == "  Ana.Perez@Example.COM "

    saved.first_name = "Cambiado"
    fetched = repo.get_profile(FAMILY_UID)
    fetched.phone = None
    original.last_name = "Otro"

    stored = repo.get_profile(FAMILY_UID)
    assert stored.first_name == "Ana"
    assert stored.last_name == "Pérez"
    assert stored.phone == "+573001234567"
    assert stored.birth_date == date(1950, 5, 1)


def test_user_repository_get_missing_profile_returns_none():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile())

    assert repo.get_profile("missing-uid") is None


def test_user_repository_add_profile_overwrites():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile(phone="+573001234567"))

    repo.add_profile(_profile(email="Nuevo@Example.com", role=Role.CUIDADOR))

    stored = repo.get_profile(FAMILY_UID)
    assert stored.email == "nuevo@example.com"
    assert stored.role == Role.CUIDADOR
    assert stored.phone is None


def test_user_repository_update_profile_applies_changes():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile(phone="+573001234567", photo_url="https://x.co/a.png"))

    updated = repo.update_profile(
        FAMILY_UID,
        {
            "first_name": "María",
            "phone": None,
            "address": "Calle 2",
            "birth_date": date(1948, 1, 2),
            "photo_url": None,
        },
    )

    assert updated == UserProfile(
        uid=FAMILY_UID,
        first_name="María",
        last_name="Pérez",
        email="ana.perez@example.com",
        role=Role.FAMILIAR,
        phone=None,
        birth_date=date(1948, 1, 2),
        address="Calle 2",
        photo_url=None,
    )
    assert repo.get_profile(FAMILY_UID) == updated

    updated.first_name = "Cambiado"
    assert repo.get_profile(FAMILY_UID).first_name == "María"


def test_user_repository_update_profile_normalizes_email():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile())

    updated = repo.update_profile(FAMILY_UID, {"email": "  NUEVO@Example.com "})

    assert updated.email == "nuevo@example.com"
    assert repo.get_profile(FAMILY_UID).email == "nuevo@example.com"


def test_user_repository_update_profile_ignores_invalid_keys():
    repo = InMemoryUserRepository()
    before = repo.add_profile(_profile())

    updated = repo.update_profile(
        FAMILY_UID,
        {"uid": "other-uid", "unknown": 1, "full_name": "X", "email": None},
    )

    assert updated == before
    assert repo.get_profile(FAMILY_UID) == before
    assert repo.get_profile("other-uid") is None


def test_user_repository_update_missing_profile_returns_none():
    repo = InMemoryUserRepository()

    assert repo.update_profile("missing-uid", {"first_name": "Ana"}) is None
    assert repo.get_profile("missing-uid") is None


def test_mark_email_verified_without_profile():
    repo = InMemoryUserRepository()
    assert repo.find_uid_by_verified_email("ana@example.com") is None

    repo.mark_email_verified(FAMILY_UID, "  Ana@Example.com ")

    assert repo.get_profile(FAMILY_UID) is None
    assert repo.find_uid_by_verified_email("ana@example.com") == FAMILY_UID
    assert repo.find_uid_by_verified_email("  ANA@EXAMPLE.COM ") == FAMILY_UID
    assert repo.find_uid_by_verified_email("otro@example.com") is None


def test_mark_email_verified_with_profile_updates_profile():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile(email="viejo@example.com"))

    repo.mark_email_verified(FAMILY_UID, "Ana@Example.com")

    profile = repo.get_profile(FAMILY_UID)
    assert profile.email_verified is True
    assert profile.email == "ana@example.com"
    assert repo.find_uid_by_verified_email("ana@example.com") == FAMILY_UID
    assert repo.find_uid_by_verified_email("viejo@example.com") is None


def test_find_uid_by_verified_email_ignores_unmarked_profiles():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile(email="ana@example.com", email_verified=True))

    assert repo.find_uid_by_verified_email("ana@example.com") is None


def test_mark_email_verified_latest_uid_wins():
    repo = InMemoryUserRepository()
    repo.mark_email_verified("old-uid", "ana@example.com")

    repo.mark_email_verified("new-uid", "ANA@example.com")

    assert repo.find_uid_by_verified_email("ana@example.com") == "new-uid"

    repo.mark_email_verified("new-uid", "otro@example.com")

    assert repo.find_uid_by_verified_email("ana@example.com") is None
    assert repo.find_uid_by_verified_email("otro@example.com") == "new-uid"


def test_mark_email_verified_ignores_blank_email():
    repo = InMemoryUserRepository()
    repo.add_profile(_profile())

    repo.mark_email_verified(FAMILY_UID, "   ")

    assert repo.find_uid_by_verified_email("") is None
    assert repo.find_uid_by_verified_email("   ") is None
    assert repo.get_profile(FAMILY_UID).email_verified is False


def test_link_caregiver_stores_and_copies_permissions():
    repo: UserRepository = InMemoryUserRepository()
    permissions = _permissions(manage_contacts=True, manage_reminders=False)

    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR, permissions)
    permissions.manage_reminders = True

    link = repo.link_of(CAREGIVER_UID, ADULT_UID)
    assert link == AdultLink(
        user_uid=CAREGIVER_UID,
        adult_uid=ADULT_UID,
        role=Role.CUIDADOR,
        permissions=_permissions(manage_contacts=True, manage_reminders=False),
    )
    assert isinstance(link.permissions, CaregiverPermissions)
    assert repo.is_linked(CAREGIVER_UID, ADULT_UID) is True

    link.permissions.receive_alerts = False
    link.role = Role.FAMILIAR

    stored = repo.link_of(CAREGIVER_UID, ADULT_UID)
    assert stored.permissions.receive_alerts is True
    assert stored.role == Role.CUIDADOR
    assert repo.link_of(CAREGIVER_UID, ADULT_UID).permissions is not stored.permissions


def test_link_caregiver_without_permissions_defaults_to_false():
    repo = InMemoryUserRepository()

    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)

    link = repo.link_of(CAREGIVER_UID, ADULT_UID)
    assert link.permissions == NO_PERMISSIONS
    assert link.permissions is not NO_CAREGIVER_PERMISSIONS

    link.permissions.receive_alerts = True

    assert NO_CAREGIVER_PERMISSIONS == NO_PERMISSIONS
    assert repo.link_of(CAREGIVER_UID, ADULT_UID).permissions == NO_PERMISSIONS
    [caregiver] = repo.caregivers_of(ADULT_UID)
    assert caregiver.permissions == NO_PERMISSIONS


def test_link_family_has_no_permissions():
    repo = InMemoryUserRepository()

    repo.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR, _permissions())

    assert repo.link_of(FAMILY_UID, ADULT_UID) == AdultLink(
        user_uid=FAMILY_UID,
        adult_uid=ADULT_UID,
        role=Role.FAMILIAR,
        permissions=None,
    )


def test_link_adult_accepts_role_value():
    repo = InMemoryUserRepository()

    repo.link_adult(CAREGIVER_UID, ADULT_UID, "cuidador")

    link = repo.link_of(CAREGIVER_UID, ADULT_UID)
    assert link.role is Role.CUIDADOR
    assert link.permissions == NO_PERMISSIONS


def test_link_of_missing_returns_none():
    repo = InMemoryUserRepository()
    repo.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)

    assert repo.link_of(FAMILY_UID, OTHER_ADULT_UID) is None
    assert repo.link_of(ADULT_UID, FAMILY_UID) is None
    assert repo.link_of("stranger-uid", ADULT_UID) is None


def test_link_adult_overwrites_pair():
    repo = InMemoryUserRepository()
    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR, _permissions())
    repo.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    new_permissions = _permissions(
        manage_contacts=False, manage_reminders=True, receive_alerts=False
    )

    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR, new_permissions)

    assert repo.link_of(CAREGIVER_UID, ADULT_UID).permissions == new_permissions
    assert repo.adults_of(CAREGIVER_UID) == [ADULT_UID]
    assert [link.user_uid for link in repo.caregivers_of(ADULT_UID)] == [CAREGIVER_UID]

    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)

    assert repo.link_of(CAREGIVER_UID, ADULT_UID).permissions == NO_PERMISSIONS
    assert repo.adults_of(CAREGIVER_UID) == [ADULT_UID]


def test_caregivers_of_returns_only_caregivers_in_insertion_order():
    repo = InMemoryUserRepository()
    second_permissions = _permissions(receive_alerts=True)
    repo.link_adult(SECOND_CAREGIVER_UID, ADULT_UID, Role.CUIDADOR, second_permissions)
    repo.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    repo.link_adult(CAREGIVER_UID, OTHER_ADULT_UID, Role.CUIDADOR, _permissions())

    caregivers = repo.caregivers_of(ADULT_UID)

    assert [link.user_uid for link in caregivers] == [
        SECOND_CAREGIVER_UID,
        CAREGIVER_UID,
    ]
    assert all(link.adult_uid == ADULT_UID for link in caregivers)
    assert all(link.role == Role.CUIDADOR for link in caregivers)
    assert caregivers[0].permissions == second_permissions
    assert caregivers[1].permissions == NO_PERMISSIONS

    caregivers[0].permissions.receive_alerts = False

    assert repo.caregivers_of(ADULT_UID)[0].permissions.receive_alerts is True
    assert repo.caregivers_of("missing-adult") == []


def test_responsible_family_of_returns_first_family_link():
    repo = InMemoryUserRepository()
    repo.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)

    assert repo.responsible_family_of(ADULT_UID) is None

    repo.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    repo.link_adult(SECOND_FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    repo.link_adult(SECOND_FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)

    assert repo.responsible_family_of(ADULT_UID) == FAMILY_UID
    assert repo.responsible_family_of(OTHER_ADULT_UID) == SECOND_FAMILY_UID
    assert repo.responsible_family_of("missing-adult") is None


def test_contact_repository_update_replaces_contact():
    repo: ContactRepository = InMemoryContactRepository()
    repo.add(ADULT_UID, _contact("contact-1", "Ana"))
    repo.add(ADULT_UID, _contact("contact-2", "Luis"))
    changed = _contact("contact-1", "Ana María").model_copy(
        update={"is_favorite": True, "favorite_order": 2, "email": "ana@example.com"}
    )

    updated = repo.update(ADULT_UID, changed)

    assert updated == changed
    assert repo.get(ADULT_UID, "contact-1") == changed
    assert [contact.id for contact in repo.list_for_adult(ADULT_UID)] == [
        "contact-1",
        "contact-2",
    ]

    changed.first_name = "Otro"
    updated.permissions.calls = False

    stored = repo.get(ADULT_UID, "contact-1")
    assert stored.first_name == "Ana María"
    assert stored.permissions.calls is True


def test_contact_repository_update_missing_returns_none():
    repo = InMemoryContactRepository()
    repo.add(ADULT_UID, _contact("contact-1", "Ana"))

    assert repo.update(ADULT_UID, _contact("missing")) is None
    assert repo.update(OTHER_ADULT_UID, _contact("contact-1", "Luis")) is None

    assert repo.get(ADULT_UID, "missing") is None
    assert repo.get(OTHER_ADULT_UID, "contact-1") is None
    assert repo.list_for_adult(OTHER_ADULT_UID) == []
    assert repo.get(ADULT_UID, "contact-1").first_name == "Ana"


def test_contact_repository_delete():
    repo = InMemoryContactRepository()
    repo.add(ADULT_UID, _contact("contact-1", "Ana"))
    repo.add(ADULT_UID, _contact("contact-2", "Luis"))
    repo.add(OTHER_ADULT_UID, _contact("contact-3", "Eva"))

    assert repo.delete(OTHER_ADULT_UID, "contact-1") is False
    assert repo.delete(ADULT_UID, "contact-1") is True

    assert repo.get(ADULT_UID, "contact-1") is None
    assert [contact.id for contact in repo.list_for_adult(ADULT_UID)] == ["contact-2"]
    assert repo.find("contact-1") is None
    assert repo.delete(ADULT_UID, "contact-1") is False
    assert repo.delete("missing-adult", "contact-2") is False
    assert [contact.id for contact in repo.list_for_adult(OTHER_ADULT_UID)] == [
        "contact-3"
    ]


def test_contact_repository_find_searches_all_adults():
    repo = InMemoryContactRepository()
    repo.add(ADULT_UID, _contact("contact-1", "Ana"))
    repo.add(OTHER_ADULT_UID, _contact("contact-2", "Luis"))

    assert repo.find("contact-2") == (OTHER_ADULT_UID, _contact("contact-2", "Luis"))

    adult_uid, contact = repo.find("contact-1")
    assert adult_uid == ADULT_UID
    assert contact == _contact("contact-1", "Ana")

    contact.first_name = "Cambiado"
    contact.permissions.help_alerts = False

    assert repo.find("contact-1")[1] == _contact("contact-1", "Ana")
    assert repo.find("missing") is None


def test_device_repository_register_is_idempotent():
    repo: DeviceRepository = InMemoryDeviceRepository()
    assert repo.has_device(FAMILY_UID) is False
    assert repo.tokens_of([FAMILY_UID]) == []

    repo.register(FAMILY_UID, "token-1")
    repo.register(FAMILY_UID, "token-1")
    repo.register(FAMILY_UID, "token-2")
    repo.register(FAMILY_UID, "token-1")

    assert repo.tokens_of([FAMILY_UID]) == ["token-1", "token-2"]
    assert repo.has_device(FAMILY_UID) is True


def test_device_repository_moves_token_between_uids():
    repo = InMemoryDeviceRepository()
    repo.register(FAMILY_UID, "shared-token")
    repo.register(FAMILY_UID, "family-token")

    repo.register(CAREGIVER_UID, "shared-token")

    assert repo.tokens_of([FAMILY_UID]) == ["family-token"]
    assert repo.tokens_of([CAREGIVER_UID]) == ["shared-token"]
    assert repo.tokens_of([FAMILY_UID, CAREGIVER_UID]) == [
        "family-token",
        "shared-token",
    ]

    repo.remove_tokens(["family-token"])

    assert repo.has_device(FAMILY_UID) is False
    assert repo.has_device(CAREGIVER_UID) is True


def test_device_repository_tokens_of_without_duplicates():
    repo = InMemoryDeviceRepository()
    repo.register(ADULT_UID, "adult-token")
    repo.register(FAMILY_UID, "family-token-1")
    repo.register(CAREGIVER_UID, "caregiver-token")
    repo.register(FAMILY_UID, "family-token-2")

    assert repo.tokens_of([FAMILY_UID, ADULT_UID, FAMILY_UID, "unknown-uid"]) == [
        "adult-token",
        "family-token-1",
        "family-token-2",
    ]
    assert repo.tokens_of(uid for uid in [CAREGIVER_UID]) == ["caregiver-token"]
    assert repo.tokens_of([]) == []
    assert repo.tokens_of(["unknown-uid"]) == []


def test_device_repository_remove_tokens():
    repo = InMemoryDeviceRepository()
    repo.register(FAMILY_UID, "token-1")
    repo.register(FAMILY_UID, "token-2")
    repo.register(CAREGIVER_UID, "token-3")

    repo.remove_tokens(["token-1", "unknown-token", "token-3"])

    assert repo.tokens_of([FAMILY_UID, CAREGIVER_UID]) == ["token-2"]
    assert repo.has_device(CAREGIVER_UID) is False

    repo.remove_tokens([])
    repo.remove_tokens(iter(["token-2"]))

    assert repo.has_device(FAMILY_UID) is False


def test_device_repository_remove_tokens_accepts_lazy_iterable_from_itself():
    repo = InMemoryDeviceRepository()
    repo.register(FAMILY_UID, "token-1")
    repo.register(CAREGIVER_UID, "token-2")
    lazy_tokens = (token for uid in [FAMILY_UID] for token in repo.tokens_of([uid]))

    worker = threading.Thread(target=repo.remove_tokens, args=(lazy_tokens,))
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert repo.has_device(FAMILY_UID) is False
    assert repo.tokens_of([CAREGIVER_UID]) == ["token-2"]


def test_device_repository_has_device():
    repo = InMemoryDeviceRepository()
    repo.register(ADULT_UID, "adult-token")

    assert repo.has_device(ADULT_UID) is True
    assert repo.has_device(FAMILY_UID) is False


def test_device_repository_provider_returns_shared_instance():
    devices = device_repository.get_device_repository()

    assert isinstance(devices, InMemoryDeviceRepository)
    assert device_repository.get_device_repository() is devices
    assert devices is device_repository._default_device_repository


def test_repositories_tolerate_concurrent_reads_and_writes():
    users = InMemoryUserRepository()
    contacts = InMemoryContactRepository()
    devices = InMemoryDeviceRepository()
    errors: list[BaseException] = []
    writes_per_thread = 150
    reads_per_thread = 40

    def writer(index: int) -> None:
        try:
            for step in range(writes_per_thread):
                users.link_adult(f"caregiver-{index}-{step}", ADULT_UID, Role.CUIDADOR)
                contacts.add(ADULT_UID, _contact(f"contact-{index}-{step}"))
                devices.register(f"uid-{index}", f"token-{index}-{step}")
        except BaseException as error:  # solo ocurre si hay una carrera
            errors.append(error)

    def reader() -> None:
        try:
            for _ in range(reads_per_thread):
                users.caregivers_of(ADULT_UID)
                users.adults_of("caregiver-0-0")
                contacts.list_for_adult(ADULT_UID)
                contacts.find("contact-0-0")
                devices.tokens_of(["uid-0", "uid-1"])
                devices.has_device("uid-2")
        except BaseException as error:  # solo ocurre si hay una carrera
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert len(users.caregivers_of(ADULT_UID)) == 4 * writes_per_thread
    assert len(contacts.list_for_adult(ADULT_UID)) == 4 * writes_per_thread
    assert len(devices.tokens_of([f"uid-{index}" for index in range(4)])) == (
        4 * writes_per_thread
    )
