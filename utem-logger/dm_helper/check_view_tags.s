// CHECK 2: does the live View image carry a frame counter or timestamp in its tags?
// Run in DM (DM-script) while a View is running, with the View window in front. It prints the
// image's whole tag tree to the Output window twice, 3 s apart, and opens the tag browser. A value
// that differs between the two prints is a per-frame counter or time the helper could read.
// No tag names are assumed: everything the image carries is printed.
// Copy the Output window text into the project.

void dump(TagGroup tg, string indent)
{
    number n = tg.TagGroupCountTags();
    for (number i = 0; i < n; i++) {
        string label = tg.TagGroupGetTagLabel(i);
        TagGroup sub;
        string sv;
        number nv;
        if (tg.TagGroupGetIndexedTagAsTagGroup(i, sub)) {
            Result(indent + label + "\n");
            dump(sub, indent + "    ");
        } else if (tg.TagGroupGetIndexedTagAsString(i, sv)) {
            Result(indent + label + " = " + sv + "\n");
        } else if (tg.TagGroupGetIndexedTagAsNumber(i, nv)) {
            Result(indent + label + " = " + nv + "\n");
        } else {
            Result(indent + label + " = (other type)\n");
        }
    }
}

image img := GetFrontImage();
Result("image: " + img.ImageGetName() + "  " + img.ImageGetDimensionSize(0) + " x " + img.ImageGetDimensionSize(1) + "\n");
TagGroup tg = img.ImageGetTagGroup();
tg.TagGroupOpenBrowserWindow(0);
for (number pass = 0; pass < 2; pass++) {
    Result("---------- pass " + pass + "\n");
    dump(tg, "");
    if (pass == 0) sleep(3);
}
