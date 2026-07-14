# export_placement.tcl
# =====================
# Export per-cell placement coordinates from a placed/routed Vivado design,
# for use with MERS physical region targeting:
#
#   analyze_regions.py  --placement placement.csv
#   run_mers.py         --use-regions --placement placement.csv
#   generate_dataset_a.py --use-regions --placement placement.csv
#
# Usage in the Vivado Tcl console (after place_design, or with a routed
# checkpoint open):
#
#   open_checkpoint your_design_routed.dcp      ;# or just place_design
#   source export_placement.tcl
#
# Produces placement.csv with rows:  cell,x,y
# where x,y are the SLICE column/row of each placed leaf cell.

set out_path "placement.csv"
set fp [open $out_path w]
puts $fp "cell,x,y"

set n 0
foreach c [get_cells -hierarchical -filter {IS_PRIMITIVE && PRIMITIVE_LEVEL != "MACRO"}] {
    set loc [get_property LOC $c]
    if {$loc eq ""} { continue }

    # Prefer explicit RPM grid coordinates if available...
    set x [get_property RPM_X $c]
    set y [get_property RPM_Y $c]

    # ...otherwise parse SLICE_X#Y# (or similar) from the site/LOC string.
    if {$x eq "" || $y eq ""} {
        if {[regexp {X(\d+)Y(\d+)} $loc -> sx sy]} {
            set x $sx
            set y $sy
        } else {
            continue
        }
    }
    puts $fp "$c,$x,$y"
    incr n
}
close $fp
puts "Wrote $n placed cells to $out_path"

# Notes
# -----
# * If your trigger/rare-node names in the .bench differ from the Vivado
#   cell names by a hierarchy prefix (e.g. design_1_i/u_core/...) or a
#   _reg suffix, that's fine -- load_placement() normalises both sides and
#   reports the match rate.
# * For DONT_TOUCH / preserved registers Vivado keeps the original net
#   name, which is what you want for matching Q_i_*_n_0 trigger nodes.
# * If you only have SLICE strings (no RPM_X/Y), the regex fallback above
#   still yields usable column/row coordinates for proximity clustering.
