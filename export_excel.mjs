// Local export only; no network and no credentials.
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, previewFlag] = process.argv.slice(2);
if (!input || !output) throw new Error('Usage: export_excel.mjs report.json output.xlsx [--preview]');
const cards = JSON.parse(await fs.readFile(input, 'utf8'));
if (!Array.isArray(cards) || cards.some(c => typeof c.iccid !== 'string' || !/^[0-9]{18,22}$/.test(c.iccid))) {
  throw new Error('ICCID must be digit strings, never JavaScript numbers');
}
const text = v => {
  const s = String(v ?? '');
  return s.startsWith('=') ? "'" + s : s;
};
const activationLabel = c => c.phase === 'verified' && c.verified_at ? '激活与信号均成功'
  : c.order_status === 'ACTIVE' && c.subscriber_status === 'ACTIVE' ? '已激活，信号待开通'
  : c.order_status === 'ACTIVATION_UNKNOWN' ? '激活结果待确认'
  : c.order_status === 'FAILED' ? '激活失败' : '尚未确认激活';
const workbook = Workbook.create();
const list = workbook.worksheets.add('ICCID清单');
const report = workbook.worksheets.add('激活核对');
const listValues = [['ICCID'], ...cards.map(c => [c.iccid])];
const reportValues = [
  ['ICCID', '激活情况', '订单号', '手机号', '订单状态', '号码状态', '信号开通状态', '核对时间（北京时间）', '备注'],
  ...cards.map(c => [c.iccid, activationLabel(c),
    text(c.order_no), text(c.phone_number), text(c.order_status), text(c.subscriber_status),
    text(c.signal_status), text(c.checked_at || c.verified_at), text(c.note)])
];
for (const [sheet, values, widths] of [[list, listValues, [30]], [report, reportValues, [30, 30, 30, 20, 16, 16, 18, 32, 65]]]) {
  const range = sheet.getRangeByIndexes(0, 0, values.length, values[0].length);
  range.setNumberFormat('@');
  range.values = values; // Every identifier remains a string before and after export.
  range.format.font = {name: 'Arial', size: 11};
  range.format.rowHeight = 23;
  range.format.verticalAlignment = 'center';
  range.format.horizontalAlignment = 'left';
  const header = sheet.getRangeByIndexes(0, 0, 1, values[0].length);
  header.format.fill = '#23445A';
  header.format.font = {name: 'Arial', size: 11, bold: true, color: '#FFFFFF'};
  header.format.horizontalAlignment = 'center';
  for (let col = 0; col < widths.length; col++) {
    sheet.getRangeByIndexes(0, col, values.length, 1).format.columnWidth = widths[col];
  }
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = false;
}
workbook.recalculate();
for (let row = 0; row < cards.length; row++) {
  if (list.getCell(row + 1, 0).values[0][0] !== cards[row].iccid ||
      report.getCell(row + 1, 0).values[0][0] !== cards[row].iccid) {
    throw new Error('ICCID precision check failed');
  }
}
await workbook.inspect({kind: 'table', range: `ICCID清单!A1:A${Math.min(cards.length + 1, 5)}`, include: 'values,formulas'});
const file = await SpreadsheetFile.exportXlsx(workbook);
const temporary = output + '.tmp.xlsx';
await file.save(temporary);
await fs.rename(temporary, output);
const privateDir = path.join(path.dirname(output), '.nexsim-live');
await fs.mkdir(privateDir, {recursive: true});
try {
  await fs.rename(temporary + '.inspect.ndjson', path.join(privateDir, 'excel-inspection.ndjson'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
if (previewFlag === '--preview') {
  const previewDir = path.join(path.dirname(output), '.nexsim-live');
  await fs.mkdir(previewDir, {recursive: true});
  for (const [sheetName, range, suffix] of [['ICCID清单', 'A1:A5', 'list'], ['激活核对', 'A1:I5', 'report']]) {
    const image = await workbook.render({sheetName, range, scale: 1, format: 'png'});
    await fs.writeFile(path.join(previewDir, `${suffix}-preview.png`), new Uint8Array(await image.arrayBuffer()));
  }
}
console.log(`Excel exported: ${cards.length} rows; ICCID stored as text.`);
