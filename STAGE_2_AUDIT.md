# Stage 2 — Market Core / Supply Routing

## What this stage now proves in software
1. Request exists before merchant activation.
2. Merchant can respond to the first request using the existing tokenized request link without building a catalog/account first.
3. Reach attempts are explicitly classified and logged.
4. A public phone number is never treated as permission for automated outreach.
5. After explicit merchant activation, a merchant becomes DIRECT for future requests.
6. Accepted/completed requests build a capability history automatically.
7. Merchant performance and capability history remain separate from commercial ranking payments.
8. No human fallback is silently inserted into the normal path.

## Reachability states
- DIRECT: explicit prior activation/consent + endpoint.
- DISCOVERED_REACHABLE: an official endpoint is discoverable, but it still needs a real adapter before claiming automatic delivery.
- DISCOVERED_UNREACHABLE: no permitted automated endpoint; skip rather than pretend.

## External constraints verified September 2026
- Egypt NTRA requires registration/activation for promotional/commercial mobile calls and has strengthened enforcement against unauthorized spam calling.
- Google Places can return place identifiers and business details, but Places content has storage/caching and attribution restrictions; Place IDs are the durable storage exception.
- Therefore this prototype deliberately does not scrape/store a permanent Google business-contact database and does not cold-call public numbers.

## Gate A status
PARTIALLY PROVED.
The internal request→discovery record→reachability classification→request link→offer→capability-learning loop is implemented.
The still-unproved part is the external transport: automatically delivering a first request to a previously unknown Egyptian business through a lawful scalable digital endpoint. No code can truthfully mark that solved without an actual provider/API/merchant endpoint.

## Stage 3 dependency
Connect at least one real transport/payment/source rail and prove it end-to-end.
