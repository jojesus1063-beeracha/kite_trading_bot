from risk_manager import circuit_proximity_pct, is_near_circuit_limit

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

# BUY: price 1267.7, upper limit 1394.4 -> distance = (1394.4-1267.7)/1267.7*100 = ~10%
d = circuit_proximity_pct("BUY", 1267.7, 1141, 1394.4)
check("BUY distance to upper circuit computed correctly (~10%)", abs(d - 9.998) < 0.1)

# SELL: price 1267.7, lower limit 1141 -> distance = (1267.7-1141)/1267.7*100 = ~10%
d2 = circuit_proximity_pct("SELL", 1267.7, 1141, 1394.4)
check("SELL distance to lower circuit computed correctly (~10%)", abs(d2 - 9.998) < 0.1)

check("Far from circuit (10%) does NOT block with 2% threshold",
      is_near_circuit_limit("BUY", 1267.7, 1141, 1394.4, 2.0) == False)

# BUY price very close to upper circuit -- should block
check("BUY very close to upper circuit (0.5% away) DOES block with 2% threshold",
      is_near_circuit_limit("BUY", 1387.5, 1141, 1394.4, 2.0) == True)

# SELL price very close to lower circuit -- should block
check("SELL very close to lower circuit (0.5% away) DOES block with 2% threshold",
      is_near_circuit_limit("SELL", 1147, 1141, 1394.4, 2.0) == True)

# Missing circuit data -- fails safe, does NOT block
check("Missing circuit data fails safe (does not block)",
      is_near_circuit_limit("BUY", 100.0, None, None, 2.0) == False)
check("circuit_proximity_pct returns None for missing data",
      circuit_proximity_pct("BUY", 100.0, None, None) is None)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
