/*
 * Copyright (c) 2008-2016, Hazelcast, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.hazelcast.simulator.utils;

import java.io.File;
import static com.hazelcast.simulator.utils.FileUtils.appendText;
import static com.hazelcast.simulator.utils.FileUtils.fileAsText;
import static com.hazelcast.simulator.utils.FileUtils.stripExtension;

@SuppressWarnings("checkstyle:magicnumber")
public final class ReportCsv {

    private ReportCsv() {
    }

    public static void main(String[] args) {
        File hgrmFile = new File(args[0]);
        File reportDir = new File(args[1]);
        File sessionFile = new File(args[2]);
        File out = new File(reportDir, "report.csv");
        if (!out.exists()) {
            FileUtils.writeText(getHeader(), out);
        }

        StringBuffer outSb = new StringBuffer();
        outSb.append(sessionFile.getName());
        outSb.append(",").append(stripExtension(hgrmFile.getName()));
        addPercentiles(hgrmFile, outSb);
        addOther(hgrmFile, outSb);
        appendText(outSb + "\n", out);
    }

    private static void addPercentiles(File hgrmFile, StringBuffer outSb) {
        String[] lines = fileAsText(hgrmFile).split("\n");
        double[] importantPercentiles = {0.1, 0.2, 0.5, 0.75, 0.90, 0.95, 0.99, 0.999, 0.9999, 1.0};
        int percentileIndex = 0;
        for (int k = 4; k < lines.length - 3; k++) {
            String line = lines[k].trim();
            if (line.isEmpty() || line.startsWith("#")) {
                continue;
            }
            String[] tokens = line.split("\\s+");
            double percentile = Double.parseDouble(tokens[1]);
            while (percentileIndex < importantPercentiles.length
                    && percentile >= importantPercentiles[percentileIndex]) {
                outSb.append(',').append(tokens[0]);
                percentileIndex++;
            }
        }

        for (int k = percentileIndex; k < importantPercentiles.length; k++) {
            outSb.append(',').append(-1);
        }
    }

    private static void addOther(File hgrmFile, StringBuffer outSb) {
        // todo: Once the new reporting is active, we can always read the .latency-history.csv
        // and remove the hgrm section.
        File file = new File(hgrmFile.getParent(), stripExtension(hgrmFile.getName()));
        if (!file.exists()) {
            file = new File(hgrmFile.getParent(), file.getName() + ".latency-history.csv");
        }

        String[] lines = fileAsText(file).split("\n");

        if (lines.length < 3) {
            // dealing with incomplete files
            return;
        }
        long startMillis = Math.round(Double.parseDouble(lines[4].split(",")[1]) * 1000);

        String lastLine = lines[lines.length - 1];

        String[] lastLineFields = lastLine.split(",");
        long operations = Long.parseLong(lastLineFields[16]);
        outSb.append(",").append(operations);

        long endMillis = Math.round(Double.parseDouble(lastLineFields[1]) * 1000);
        long durationMillis = endMillis - startMillis;

        outSb.append(",").append(durationMillis);

        double throughput = operations * 1000d / durationMillis;
        outSb.append(",").append(throughput);
    }

    private static String getHeader() {
        return "\"run_label\","
                + "\"benchmark\","
                + "\"10%(us)\","
                + "\"20%(us)\","
                + "\"50%(us)\","
                + "\"75%(us)\","
                + "\"90%(us)\","
                + "\"95%(us)\","
                + "\"99%(us)\","
                + "\"99.9%(us)\","
                + "\"99.99%(us)\","
                + "\"max(us)\","
                + "\"operations\","
                + "\"duration(ms)\","
                + "\"throughput\"\n";
    }
}
