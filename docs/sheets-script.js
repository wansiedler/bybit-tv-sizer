// Apps Script web app for the lexx-relay trade journal.
// Lives in the spreadsheet (Extensions -> Apps Script), not executed here.
// SECRET must match SHEETS_SECRET in bipboop.

const SECRET = "put-your-own-long-random-string-here"; // pragma: allowlist secret

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (d.secret !== SECRET) return ContentService.createTextOutput("no");
  const sheet = SpreadsheetApp.getActive().getSheets()[0];
  if (d.headers) {
    if (String(sheet.getRange(1, 1).getValue()) !== d.headers[0]) sheet.insertRowBefore(1);
    sheet.getRange(1, 1, 1, d.headers.length).setValues([d.headers]).setFontWeight("bold");
    return ContentService.createTextOutput("ok");
  }
  if (d.totals) {
    if (String(sheet.getRange(2, 1).getValue()) !== "ИТОГО") sheet.insertRowBefore(2);
    sheet.getRange(2, 1).setValue("ИТОГО");
    sheet.getRange(2, 4).setFormula(
      '=COUNTIFS(D3:D,"win",A3:A,"<>ИТОГ*")&" / "&COUNTIFS(D3:D,"stop",A3:A,"<>ИТОГ*")');
    sheet.getRange(2, 6).setFormula('=SUMIF(A3:A,"<>ИТОГ*",F3:F)');
    sheet.getRange(2, 11).setFormula('=SUMIF(A3:A,"<>ИТОГ*",K3:K)');
    sheet.getRange(2, 12).setFormula('=SUMIF(A3:A,"<>ИТОГ*",L3:L)');
    sheet.getRange(2, 1, 1, 15).setFontWeight("bold")
      .setBackground("#434343").setFontColor("#ffffff");
    sheet.setFrozenRows(2);
    return ContentService.createTextOutput("ok");
  }
  if (d.week) {
    weekSummary_(sheet);
    return ContentService.createTextOutput("ok");
  }
  sheet.appendRow(d.row);
  if (d.png) {
    const blob = Utilities.newBlob(Utilities.base64Decode(d.png), "image/png",
                                   (d.name || "trade") + ".png");
    const file = folder_().createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    const r = sheet.getLastRow();
    try {
      chip_(sheet, r, 7, file.getUrl());
    } catch (err) {
      sheet.getRange(r, 7).setValue(file.getUrl());
    }
  }
  return ContentService.createTextOutput("ok");
}

function chip_(sheet, row, col, url) {
  Sheets.Spreadsheets.batchUpdate({
    requests: [{
      updateCells: {
        start: { sheetId: sheet.getSheetId(), rowIndex: row - 1, columnIndex: col - 1 },
        fields: "chipRuns,userEnteredValue",
        rows: [{
          values: [{
            userEnteredValue: { stringValue: "@" },
            chipRuns: [{ startIndex: 0, chip: { richLinkProperties: { uri: url } } }]
          }]
        }]
      }
    }]
  }, SpreadsheetApp.getActive().getId());
}

function weekSummary_(sheet) {
  const last = sheet.getLastRow();
  if (last < 3) return;
  const a = sheet.getRange(3, 1, last - 2, 1).getValues();
  let start = 3;
  for (let i = 0; i < a.length; i++) {
    if (String(a[i][0]).indexOf("ИТОГ НЕДЕЛИ") === 0) start = i + 4;
  }
  if (start > last) return;
  const rows = sheet.getRange(start, 1, last - start + 1, 6).getValues();
  let sumR = 0, wins = 0, losses = 0;
  rows.forEach(r => {
    if (r[3] === "win") wins++;
    if (r[3] === "stop") losses++;
    sumR += Number(r[5]) || 0;
  });
  const row = last + 1;
  sheet.appendRow(["ИТОГ НЕДЕЛИ", "", "", sumR >= 0 ? "WIN" : "LOSS",
                   "win " + wins + " / stop " + losses,
                   Math.round(sumR * 100) / 100]);
  sheet.getRange(row, 1, 1, 15).setBackground("#38761d").setFontColor("#ffffff");
}

function authTest() { folder_(); }

function folder_() {
  const it = DriveApp.getFoldersByName("trade-screens");
  return it.hasNext() ? it.next() : DriveApp.createFolder("trade-screens");
}
