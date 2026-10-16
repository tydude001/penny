# Sets a month of Sure budgets from home/assumptions.toml [budget] (no API route writes budgets).
# Dry run: docker exec sure-web bin/rails runner /tmp/budgets.rb   Apply: add -e APPLY=1.
# payments_off_the_feed (150) has no Sure category and is left out; total 4870.
MAP = {
  "Food & Drink" => 490, "Entertainment" => 100, "Fees" => 60, "Gifts & Donations" => 0,
  "Groceries" => 610, "Healthcare" => 40, "Mortgage / Rent" => 1780, "Insurance" => 90,
  "Personal Care" => 30, "Services" => 40, "Shopping" => 510, "Subscriptions" => 120,
  "Taxes" => 140, "Transportation" => 560, "Travel" => 90, "Utilities" => 210,
}
apply = ENV["APPLY"] == "1"
puts "families=#{Family.count}"
family = Family.first
user = family.users.order(:created_at).first
puts "personal_budgets=#{family.respond_to?(:personal_budgets?) ? family.personal_budgets? : 'n/a'} users=#{family.users.count}"
cats = family.categories.where(name: MAP.keys).index_by(&:name)
puts "missing=#{MAP.keys - cats.keys}"
start = Date.new(2026, 10, 1)
if !apply
  b = Budget.where(family: family, start_date: start).first
  puts "october exists=#{!b.nil?} initialized=#{b&.initialized?}"
  puts "total=#{MAP.values.sum}"
  exit
end
budget = Budget.find_or_bootstrap(family, start_date: start, user: user)
budget.update!(budgeted_spending: MAP.values.sum)
MAP.each do |name, amt|
  bc = budget.budget_categories.find_by!(category_id: cats.fetch(name).id)
  bc.update_budgeted_spending!(amt)
end
Budget::RolloverCalculator.new(family: family, user: budget.user).recompute!
budget.reload
puts "#{budget.name} budgeted=#{budget.budgeted_spending} allocated=#{budget.allocated_spending} valid=#{budget.allocations_valid?}"
budget.budget_categories.includes(:category).each { |bc| puts "  #{bc.category.name}: #{bc.budgeted_spending}" if bc.budgeted_spending.to_f > 0 }
