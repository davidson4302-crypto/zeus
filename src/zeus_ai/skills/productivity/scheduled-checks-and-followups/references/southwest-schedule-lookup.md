# Southwest schedule lookup

Use when the flight to track is on Southwest and is not a reservation in the owner's name. Read only. Do not select a fare, continue, or start a purchase.

## Account trips are the wrong page

- `https://www.southwest.com/myaccount/upcoming-trips` returns a not-found page. Do not treat that as "no trips."
- The signed-in account home is `https://www.southwest.com/loyalty/myaccount/`. Upcoming-trip cards there are reservations on that Rapid Rewards account only.
- Someone else's booking will not appear on the owner's account. A missing card is not a cancellation and not proof the flight was never booked.

## Public schedule search

Open this URL. Substitute the origin code, destination code, and `YYYY-MM-DD`.

https://www.southwest.com/air/booking/select.html?adultPassengersCount=1&departureDate=YYYY-MM-DD&departureTimeOfDay=ALL_DAY&destinationAirportCode=<DEST>&fareType=USD&originationAirportCode=<ORIGIN>&passengerType=ADULT&tripType=oneway

It redirects to `/air/booking/select-depart` and lists flight cards. Each card has flight numbers (`WN####`, or `WN#### / WN####` when there is a connection), a depart clock time, an arrive clock time, stop count, connection airport when shown (for example `plane change` plus the airport), and duration.

Being signed in does not change this listing into the owner's reservations. Ignore prices. They are current offers, not what was paid.

## How to match

- Departure clock time is local to the origin airport. Arrival clock time is local to the destination airport.
- Read AM/PM from the card. A bare clock time is not morning.
- Take the unique card whose stop count and arrival time match the description. A named connection city is supporting detail unless the owner named the stop.
- Record flight numbers, both local times, date, stop count, and connection airport. Those go into the watch prompt.
- The listing does not show passenger name, confirmation, or seat. Do not claim those.
- If more than one card still fits, name the candidates in one line and ask which one. Do not pick a fare to narrow them.

## Live status later

The select-depart page is a shop listing, not live flight status. On flight day, start with the dated tracker at `https://www.flightstats.com/v2/flight-tracker/WN/<number>?year=YYYY&month=M&date=D`. Read the status block for the matching date and airport pair; if it shows another leg, select the correct entry from the dated flights on that page. Save the leg-specific URL with `flightId` when available so a later check opens the same leg. Keep scheduled, estimated, and actual times separate: an “On time” label with a blank estimate is not a fresh ETA. If the tracker can't verify the leg, check Southwest's flight-status page or the relevant airport board for the same date and route. Don't treat a fare or “unavailable” in the shop as a delay or cancellation.
