# Publishing Minerva's Scribe to the Chrome Web Store

## One-time setup

1. Register a Chrome Web Store developer account at
   https://chrome.google.com/webstore/devconsole/register
   (costs $5, one-time)

2. Verify your email if prompted.

## Packaging the extension

From the project root, run:

```
./package.sh
```

This creates `minervas-scribe.zip` (about 28KB).

## Uploading to the Chrome Web Store

1. Go to https://chrome.google.com/webstore/devconsole
2. Click **New Item**
3. Upload `minervas-scribe.zip`
4. Fill in the listing:
   - **Name**: Minerva's Scribe
   - **Short description**: copy from `extension/store/description.txt` (the SHORT DESCRIPTION section)
   - **Detailed description**: copy from `extension/store/description.txt` (the DETAILED DESCRIPTION section)
   - **Category**: Productivity
   - **Language**: English
   - **Icon**: already included in the ZIP (128x128)
   - **Screenshots**: upload `extension/store/screenshot_1280x800.png`
5. Under **Distribution** > **Visibility**, select **Unlisted**
6. Click **Submit for Review**

Review usually takes 1-3 business days for unlisted extensions.

## After approval

You'll get a direct install link:
```
https://chrome.google.com/webstore/detail/minervas-scribe/YOUR_EXTENSION_ID
```

Share that link with professors. They click it, hit "Add to Chrome", and they're set. No other setup needed on their end.

## Pushing updates

When you make code changes:

1. Bump the `version` in `extension/manifest.json` (e.g. `"2.0.0"` -> `"2.0.1"`)
2. Run `./package.sh`
3. Go to the Developer Dashboard, click your extension, click **Package** > **Upload new package**
4. Upload the new ZIP
5. Submit for review

Chrome auto-updates extensions every few hours, so users will get the new version without doing anything.
