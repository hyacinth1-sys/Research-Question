global descvars approved ai_exposure race_cat loan_type ///
    loan_purpose income occupancy_type per_capita_income_usd annual_employment


estpost summarize $descvars, detail

esttab using "Descriptive_Statistics.rtf", ///
    cells("count(fmt(0)) mean(fmt(3)) sd(fmt(3)) p25(fmt(3)) p50(fmt(3)) p75(fmt(3))") ///
    noobs nonumber nomtitle ///
    collabels("N" "Mean" "SD" "P25" "Median" "P75") ///
    label ///
    replace
reghdfe approved ai_exposure race_cat loan_type loan_purpose income  occupancy_type construction_method, absorb(county_id bank_id#activity_year) cluster(county_id )
est store m2
estadd local Absorb "county_id bank_id#activity_year"
estadd local Cluster "county_id bank_id "
gen sample_approved = e(sample)
reghdfe approved ai_exposure if sample_approved ==1 , absorb(county_id bank_id#activity_year) cluster(county_id bank_id)
est store m1 
estadd local Absorb "county_id bank_id#activity_year"
estadd local Cluster "county_id  "
esttab  m1 m2  using "ai and LOAN1.rtf", ///
    replace ///
    cells(b(star fmt(3)) t(par fmt(2))) ///
    star(* 0.10 ** 0.05 *** 0.01) ///
    mtitles("approved" "approved") ///
    stats(Absorb Cluster N r2_a, ///
          labels("Fixed effects" "Cluster" "Observations" "Adjusted R-squared") ///
          fmt(%s %s %9.0fc %9.3f))


reghdfe per_capita_income_usd ai_exposure race_cat loan_type loan_purpose income  occupancy_type construction_method, absorb(county_id bank_id#activity_year) cluster(county_id )
est store m3
estadd local Absorb "county_id bank_id#activity_year"
estadd local Cluster "county_id  "
reghdfe annual_employment ai_exposure race_cat loan_type loan_purpose income  occupancy_type construction_method, absorb(county_id bank_id#activity_year) cluster(county_id )
est store m4
estadd local Absorb "county_id bank_id#activity_year"
estadd local Cluster "county_id  "
esttab  m3 m4  using "ai and income2.rtf", ///
    replace ///
    cells(b(star fmt(3)) t(par fmt(2))) ///
    star(* 0.10 ** 0.05 *** 0.01) ///
    mtitles( "Per capita income" "annual_employment") ///
    stats(Absorb Cluster NPer capita income r2_a, ///
          labels("Fixed effects" "Cluster" "Observations" "Adjusted R-squared") ///
          fmt(%s %s %9.0fc %9.3f))
		 