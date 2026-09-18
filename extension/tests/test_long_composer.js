const {loadAdapters, test, assertEqual, report} = require("./harness.js");
const {StubElement} = require("./dom_stub.js");

async function main() {
  await test("contenteditable long prompts are appended chunk-by-chunk without loss", () => {
    loadAdapters([]);
    const composer = new StubElement("div", {contenteditable: "true"});
    document._composerTarget = composer;
    const prompt = "HEAD_01" + "A".repeat(40_000) + "MID_02" + "B".repeat(40_000) + "TAIL_03";
    FancyGPTSiteKit.setComposer(composer, prompt);
    assertEqual(composer.textContent, prompt, "long contenteditable prompt must survive chunked editor writes");
  });

  await test("textarea long prompts are appended without loss", () => {
    loadAdapters([]);
    const composer = new StubElement("textarea");
    const prompt = "HEAD_T" + "C".repeat(50_000) + "MID_T" + "D".repeat(50_000) + "TAIL_T";
    FancyGPTSiteKit.setComposer(composer, prompt);
    assertEqual(composer._text, prompt, "long textarea prompt must survive chunked writes");
  });

  report();
}

main();
