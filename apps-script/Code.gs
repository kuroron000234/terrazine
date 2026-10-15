// TERRAZINE を script.google.com 経由で表示するための Google Apps Script。
// 会社のネットワークで github.io / pages.dev が「未分類」として開けないため、
// Google のサーバー側でページを取得して、そのまま返す。
// データは毎時 GitHub Actions が更新するので、このスクリプトを変更する必要はない。
function doGet() {
  var url = "https://kuroron000234.github.io/terrazine/index.html";
  var res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) {
    return HtmlService.createHtmlOutput("TERRAZINE error: " + res.getResponseCode());
  }
  return HtmlService.createHtmlOutput(res.getContentText("UTF-8"))
    .setTitle("TERRAZINE")
    .addMetaTag("viewport", "width=device-width, initial-scale=1")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}
