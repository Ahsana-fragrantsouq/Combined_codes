// backfill shopify order id in airtable (#2034)
// ONE-TIME BACKFILL SCRIPT
// Run this as a Scripting extension (Extensions -> Scripting -> paste
// this in -> Run). Do NOT use this inside an Automation - automations
// have a 30-second time limit, this script needs longer for large
// order counts.
//
// What it does: finds every Orders record that has an "Order ID"
// (Shopify's internal numeric id) but no "Shopify order id" yet,
// looks up the real order number from Shopify's API, and fills it in.

// Paste your Render app's base URL here, e.g. "https://combined-codes.onrender.com"
let PROXY_BASE_URL = "PASTE_YOUR_RENDER_URL_HERE";

let ordersTable = base.getTable("Orders");

let query = await ordersTable.selectRecordsAsync({
    fields: ["Order ID", "Shopify order id"]
});

let toProcess = query.records.filter(function (r) {
    let orderId = r.getCellValueAsString("Order ID");
    let existing = r.getCellValueAsString("Shopify order id");
    return orderId && !existing;
});

output.text(`Found ${toProcess.length} orders to backfill.`);

let updates = [];
let processed = 0;
let failed = 0;
let checked = 0;

for (let record of toProcess) {
    checked++;
    let shopifyOrderId = record.getCellValueAsString("Order ID");

    try {
        let url = `${PROXY_BASE_URL}/shopify/order-number/${shopifyOrderId}`;
        let resp = await fetch(url);

        if (resp.status === 429) {
            // Rate limited - back off and retry once
            await new Promise(function (resolve) { setTimeout(resolve, 2000); });
            resp = await fetch(url);
        }

        let rawText = await resp.text();

        if (!resp.ok) {
            console.log(`HTTP ${resp.status} for Order ID ${shopifyOrderId}: ${rawText}`);
            failed++;
            continue;
        }

        let data;
        try {
            data = JSON.parse(rawText);
        } catch (parseErr) {
            console.log(`Non-JSON response for Order ID ${shopifyOrderId}: ${rawText}`);
            failed++;
            continue;
        }

        let orderNumber = data && data.order_number;

        if (orderNumber) {
            updates.push({
                id: record.id,
                fields: { "Shopify order id": "#" + orderNumber }
            });
            processed++;
        } else {
            console.log(`No order_number for Order ID ${shopifyOrderId}, response was: ${rawText}`);
            failed++;
        }
    } catch (e) {
        console.log(`Exception for Order ID ${shopifyOrderId}: ${e && e.message ? e.message : String(e)}`);
        failed++;
    }

    // Throttle to stay comfortably under Shopify's rate limit
    await new Promise(function (resolve) { setTimeout(resolve, 550); });

    // Write in batches of 50 (Airtable's per-call limit)
    if (updates.length === 50) {
        await ordersTable.updateRecordsAsync(updates);
        output.text(`Progress: checked ${checked}/${toProcess.length}, updated ${processed}, failed ${failed}`);
        updates = [];
    }
}

// Flush any remaining updates
if (updates.length > 0) {
    await ordersTable.updateRecordsAsync(updates);
}

output.text(`DONE. Checked: ${toProcess.length}, Updated: ${processed}, Failed: ${failed}`);