// Optional real-browser acceptance for the computed nine-renderer gallery.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.BIOSIMULANT_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.argv[2];
    await page.goto(base);
    const response = await page.request.post(`${base}/api/runs`, { data: {} });
    assert.equal(response.status(), 201);
    const runId = (await response.json()).data.run.id;
    const deadline = Date.now() + 30000;
    let run;
    do {
      run = (await (await page.request.get(`${base}/api/runs/${runId}`)).json()).data.run;
      if (run.status === 'completed' || run.status === 'failed') break;
      await new Promise(resolve => setTimeout(resolve, 100));
    } while (Date.now() < deadline);
    assert.equal(run.status, 'completed', run.error);
    const results = (await (await page.request.get(`${base}/api/runs/${runId}/results`)).json()).data.results;
    assert.equal(results.visualization.status, 'passed');
    assert.equal(results.outputs.result.count.value, 12);
    // Reload verifies result discovery and rendering from the persisted run.
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('.visual-card').length === 9);
    const types = await page.locator('.renderer-badge').allTextContents();
    assert.deepEqual(types.sort(), ['bar', 'graph', 'heatmap', 'image', 'scatter', 'structure3d', 'table', 'text', 'timeseries']);
    await page.waitForFunction(() => {
      const image = document.querySelector('.image-visual');
      return image && image.complete && image.naturalWidth > 0;
    });
    assert.equal(await page.locator('.heatmap td').count(), 2);
    assert.equal(await page.locator('.graph-visual circle').count(), 2);
    assert.ok((await page.locator('.chart').allTextContents()).join(' ').includes('Population (CFU)'));
    assert.ok((await page.locator('.chart').allTextContents()).join(' ').includes('Observed (CFU)'));
    await page.waitForFunction(() => document.querySelector('.structure-viewport canvas') && !document.querySelector('.structure-overlay'), null, { timeout: 30000 });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ runId, rendered: types, imageLoaded: true, structureLoaded: true }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
