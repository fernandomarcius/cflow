"""Modeling: cubes (dimensions + named measures) and views (pivot). Versioned JSON definitions.

Principle: every number comes from a NAMED MEASURE of the cube; there is no free-form formula.
The query aggregates the snapshot of the source pipeline's execution; the page/portal only
references the model.
"""
