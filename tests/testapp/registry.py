from ..testapp_shared import models as shared
from . import models as m


STRATEGIES = {
    "negative_id": {
        "Person": m.Person,
        "Address": m.Address,
        "Phone": m.Phone,
        "PersonSource": shared.PersonSource,
        "AddressSource": shared.AddressSource,
        "PhoneSource": shared.PhoneSource,
        "PersonAddressThrough": m.PersonAddressThrough,
        "PersonPhoneThrough": m.PersonPhoneThrough,
        "PersonProfile": m.PersonProfile,
        "AddressNote": m.AddressNote,
        "PhoneTag": m.PhoneTag,
    },
    "uuid4": {
        "Person": m.PersonUuid4,
        "Address": m.AddressUuid4,
        "Phone": m.PhoneUuid4,
        "PersonSource": shared.PersonSourceUuid4,
        "AddressSource": shared.AddressSourceUuid4,
        "PhoneSource": shared.PhoneSourceUuid4,
        "PersonAddressThrough": m.PersonAddressThroughUuid4,
        "PersonPhoneThrough": m.PersonPhoneThroughUuid4,
        "PersonProfile": m.PersonProfileUuid4,
        "AddressNote": m.AddressNoteUuid4,
        "PhoneTag": m.PhoneTagUuid4,
    },
    "uuid7_polyfill": {
        "Person": m.PersonUuid7Polyfill,
        "Address": m.AddressUuid7Polyfill,
        "Phone": m.PhoneUuid7Polyfill,
        "PersonSource": shared.PersonSourceUuid7Polyfill,
        "AddressSource": shared.AddressSourceUuid7Polyfill,
        "PhoneSource": shared.PhoneSourceUuid7Polyfill,
        "PersonAddressThrough": m.PersonAddressThroughUuid7Polyfill,
        "PersonPhoneThrough": m.PersonPhoneThroughUuid7Polyfill,
        "PersonProfile": m.PersonProfileUuid7Polyfill,
        "AddressNote": m.AddressNoteUuid7Polyfill,
        "PhoneTag": m.PhoneTagUuid7Polyfill,
    },
}
