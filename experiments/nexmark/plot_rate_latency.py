#!/usr/bin/env python3

# ./plot_latency_timeline.py results/98f4e2fa2e8bc839/ "[ ('bin_shift', 8), ('duration', 120), ('machine_local', True), ('processes', 2), ('workers', 8), ]"

import sys, os, shutil, json
import argparse
import plot
from collections import defaultdict

parser = argparse.ArgumentParser(description="Plot")
parser.add_argument('results_dir')
parser.add_argument('filtering')
parser.add_argument('primary_group')
parser.add_argument('--json', action='store_true')
parser.add_argument('--gnuplot', action='store_true')
parser.add_argument('--terminal', default="pdf")
parser.add_argument('--filter', nargs='+', default=[])
parser.add_argument('--rename', default="{}")
parser.add_argument('--name')
args = parser.parse_args()
print(args, file=sys.stderr)

results_dir = args.results_dir
files = plot.get_files(results_dir)
# for f in files:
#      print(f[1])
filtering = eval(args.filtering)
rename = eval(args.rename)

if len(args.filter) > 0:
    graph_filtering = []
    data = []
    experiments = []
    for filter in args.filter:
        filter = eval(filter)
        # print(filtering+filter, file=sys.stderr)
        gf, da, ex = plot.latency_plots(results_dir, files, filtering + filter)
        for f in ex:
            # print(f, file=sys.stderr)
            f.extend(filter)
        graph_filtering.extend(gf)
        data.extend(da)
        experiments.extend(ex)
    # print("experiments", experiments, file=sys.stderr)
else:
    graph_filtering, data, experiments = plot.latency_plots(results_dir, files, filtering)
# print(type(graph_filtering), type(data), type(experiments), file=sys.stderr)

commit = results_dir.rstrip('/').split('/')[-1]
# print("commit:", commit, file=sys.stderr)

if len(data) == 0:
    print("No data found: {}".format(filtering), file=sys.stderr)
    exit(0)


# Try to extract duration from filter pattern
duration = next((x[1] for x in filtering if x[0] == 'duration'), 450)

plot.ensure_dir("charts/{}".format(commit))

if args.json:
    extension = "json"
elif args.gnuplot:
    extension = "gnuplot"
else:
    extension = "html"

plot_name = plot.kv_to_string(dict(graph_filtering))

def get_chart_filename(extension):
    name = ""
    if args.name:
        name = "_{}".format(args.name)
    graph_filename = "{}{}+{}.{}".format(plot.plot_name(__file__), name, plot_name, extension)
    return "charts/{}/{}".format(commit, graph_filename)

chart_filename = get_chart_filename(extension)
# title = ", ".join("{}: {}".format(k, v) for k, v in sorted(graph_filtering.items(), key=lambda t: t[0]))
title = plot.kv_to_name(graph_filtering)

# for ds in data:
#     print(ds)

if args.gnuplot:

    def get_key(config, key):
        for k, v in config:
            if k == key:
                return v
        return None

    # Generate dataset
    all_headers = set()

    all_primary = defaultdict(list)

    for ds in data:
        for d in ds:
            for k in d.keys():
                all_headers.add(k)
    for config, ds in zip(iter(experiments), iter(data)):
        all_primary[get_key(config, args.primary_group)].append((config, ds))

    if len(all_primary) < 1:
        print("nothing found", file=sys.stderr)
        print("all_primary: {}".format(all_primary.keys()), file=sys.stderr)
        exit(0)

    all_headers.remove("experiment")
    all_headers = sorted(all_headers)

    migration_to_index = defaultdict(list)

    graph_filtering_bin_shift = dict(graph_filtering)

    dataset_filename = get_chart_filename("dataset")

    all_configs = []

    def format_key(group, key):
        print(group, key, file=sys.stderr)
        if group == "domain":
            return "{}M".format(int(key/1000000))
        if key in rename:
            key = rename[key]
        return str(key)

    all_rates = set()

    with open(dataset_filename, 'w') as c:
        index = 0
        # print(" ".join(all_headers), file=c)
        for key, item in sorted(all_primary.items()):
            key = format_key(args.primary_group, key)
            print("\"{}\"".format(str(key).replace("_", "\\\\_")), file=c)
            for config, ds in item:
                all_configs.append(config)
                all_rates.add(dict(config)['rate'] / 1000000)
                print(config, file=sys.stderr)
                # print("\"{}\"".format(plot.kv_to_name(config).replace("_", "\\\\_")), file=c)
                print(" ".join(map(plot.quote_str, [ds[-1][k] for k in all_headers])), file=c)
                # print("\n", file=c)
            index += 1
            print("\n", file=c)

    config_reorder = sorted(map(lambda x: (x[1], x[0]), enumerate(all_configs)))

    # Generate gnuplot script
    gnuplot_terminal = args.terminal
    gnuplot_out_filename = get_chart_filename(gnuplot_terminal)
    print(all_headers, file=sys.stderr)
    latency_index = all_headers.index("latency") + 1
    rate_index = all_headers.index("rate") + 1
    # fix long titles
    if len(title) > 79:
        idx = title.find(" ", int(len(title) / 2))
        if idx != -1:
            title = "{}\\n{}".format(title[:idx], title[idx:])
    with open(chart_filename, 'w') as c:
        print("""\
set terminal {gnuplot_terminal} font \"TimesNewRoman, 16\"
set logscale y
set logscale x

# set format y "10^{{%T}}"
# set format x "%s*10^6"
set grid xtics ytics

# set size square

# set xrange [10**7:10**11]

set output '{gnuplot_out_filename}'
stats '{dataset_filename}' using 0 nooutput
if (STATS_blocks == 0) exit
set for [i=1:STATS_blocks] linetype i dashtype i
# set for [i=1:STATS_blocks] linetype i dashtype i
# set title "{title}"
# set yrange [10**floor(log10(STATS_min)): 10**ceil(log10(STATS_max))]
set xrange [2.5*10**-1:4*10**1]
set yrange [10**-1: 10**3]
set xtics ({rates})

set ylabel "Max latency [s]"
set xlabel "Rate [million records/s]"
# set origin 0, 0
# set lmargin 0
# set rmargin at screen .70
set key at screen .5, screen 0.01 center bottom maxrows 1 maxcols 10 
set bmargin at screen 0.24
plot for [i=0:*] '{dataset_filename}' using (${rate_index}/1000000):(${latency_index}/1000) index (i+0) with linespoints title columnheader(1)
        """.format(dataset_filename=dataset_filename,
                   gnuplot_terminal=gnuplot_terminal,
                   gnuplot_out_filename=gnuplot_out_filename,
                   rate_index=rate_index,
                   latency_index=latency_index,
                   title=title.replace("_", "\\\\_"),
                   duration=duration,
                   num_plots=len(migration_to_index),
                   key2=args.primary_group.replace("_", " "),
                   rates=",".join(sorted(map(str, all_rates))),
                   ), file=c)

else: # json or html

    vega_lite = {
        "$schema": "https://vega.github.io/schema/vega-lite/v2.json",
        "title": ", ".join("{}: {}".format(k, v) for k, v in sorted(graph_filtering, key=lambda t: t[0])),
        "facet": {
            "column": {"field": "experiment", "type": "nominal"},
            "row": {"field": "queries", "type": "nominal"},
        },
        "spec": {
            "width": 600,
            "layer": [
                {
                    "mark": {
                        "type": "line",
                        "clip": True,
                    },
                    "encoding": {
                        "x": {"field": "time", "type": "quantitative", "axis": {"labelAngle": -90},
                              "scale": {"domain": [duration / 2 - 20, duration / 2 + 20]}},
                        # "x": { "field": "time", "type": "quantitative", "axis": { "labelAngle": -90 }, "scale": {"domain": [duration / 2 - 20, duration / 2 + 20]} },
                        "y": {"field": "latency", "type": "quantitative", "axis": {"format": "e", "labelAngle": 0},
                              "scale": {"type": "log"}},
                        "color": {"field": "p", "type": "nominal"},
                    },
                },
                {
                    "mark": "rule",
                    "encoding": {
                        "x": {
                            "aggregate": "mean",
                            "field": "time",
                            "type": "quantitative",
                        },
                        "color": {"value": "grey"},
                        "size": {"value": 1}
                    }
                }
            ]
        },
        "data": {
            "values": data
        },
    };

    if args.json:
        with open(chart_filename, 'w') as c:
            print(json.dumps(vega_lite), file=c)
    else:
        print(sorted(graph_filtering, key=lambda t: t[0]))
        vega_lite["title"] = title
        html = """
        <!DOCTYPE html>
        <html>
        <head>
          <script src="https://cdn.jsdelivr.net/npm/vega@3"></script>
          <script src="https://cdn.jsdelivr.net/npm/vega-lite@2"></script>
          <script src="https://cdn.jsdelivr.net/npm/vega-embed@3"></script>
        </head>
        <body>

          <div id="vis"></div>

          <script type="text/javascript">
            const vega_lite_spec = """ + \
               json.dumps(vega_lite) + \
               """

                   vegaEmbed("#vis", vega_lite_spec, { "renderer": "svg" });
                 </script>
               </body>
               </html>
               """
        with open(chart_filename, 'w') as c:
            print(html, file=c)

print(chart_filename)
print(os.getcwd() + "/" + chart_filename, file=sys.stderr)
