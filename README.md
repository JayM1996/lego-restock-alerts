# LEGO restock alerts

Every 15 minutes (07:00-23:00 UK time) this checks LEGO.com and Very for the sets in `stock_watch.json` and sends a phone
alert (ntfy) when one comes back in stock. It also reads the public HotUKDeals LEGO feed for new matching deals.
To watch another set, add it to `stock_watch.json`. The phone-alert topic is a repository secret (NTFY_TOPIC).
