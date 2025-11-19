# Copyright (c) 2022, phamos.eu and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, getdate, add_days, formatdate, flt
from frappe.utils.data import date_diff, time_diff_in_hours
from frappe.query_builder import DocType
from pypika import Order
from pypika.functions import Date
from hrms.hr.utils import get_holiday_dates_for_employee
import traceback

class Workday(Document):
	def validate(self):
		# Skip automatic data fetching/manipulation if coming from API with skip_auto_fetch flag
		if not getattr(self, 'skip_auto_fetch', False):
			self.set_actual_employee_log()
			self.date_is_in_comp_off()
			self.set_status_for_leave_application()
		self.validate_duplicate_workday()

	def set_actual_employee_log(self):
		new_workday_dict = get_actual_employee_log(self.employee, self.log_date)
		self.employee_checkins = []

		self.hours_worked = new_workday_dict.get("hours_worked")
		self.break_hours = new_workday_dict.get("break_hours")
		self.target_hours = new_workday_dict.get("target_hours")
		self.expected_break_hours = new_workday_dict.get("expected_break_hours")
		self.manual_workday = new_workday_dict.get("manual_workday")
		self.actual_working_hours = new_workday_dict.get("actual_working_hours")
		self.first_checkin = new_workday_dict.get("first_checkin")
		self.last_checkout = new_workday_dict.get("last_checkout")
		self.attendance = new_workday_dict.get("attendance")
		self.status = new_workday_dict.get("status")

		employee_checkins = new_workday_dict.get("employee_checkins") or []

		for employee_checkin in employee_checkins:
			self.append("employee_checkins", {
				"employee_checkin": employee_checkin.get("name"),
				"log_type": employee_checkin.get("log_type"),
				"log_time": employee_checkin.get("time"),
				"skip_auto_attendance": employee_checkin.get("skip_auto_attendance"),
			})

	def set_status_for_leave_application(self):
		filters = {
			"employee": self.employee,
			"from_date": ("<=", self.log_date),
			"to_date": (">=", self.log_date),
			"docstatus": 1
		}
		leave_types = frappe.get_all("Leave Type", filters={"is_compensatory": 1}, pluck="name")
		if leave_types:
			filters["leave_type"] = ['not in', leave_types]

		leave_application = frappe.db.exists("Leave Application", filters)
		if leave_application :
			self.target_hours = 0
			self.expected_break_hours= 0
			self.actual_working_hours= 0
			self.status = "On Leave"

		if (self.status == 'Half Day'):
			self.target_hours = self.target_hours / 2
		elif (self.status == 'On Leave'):
			self.target_hours = 0

	def date_is_in_comp_off(self):
		leave_types = frappe.get_all("Leave Type", filters={"is_compensatory": 1}, pluck="name")
		if not leave_types:
			return

		leave_application = frappe.db.exists(
			"Leave Application", {
				"employee": self.employee,
				"from_date": ("<=", self.log_date),
				"to_date": (">=", self.log_date),
				"leave_type": ["in", leave_types],
				"docstatus": 1
			}
		)

		if leave_application:
			self.hours_worked = 0.0
			self.actual_working_hours = -self.target_hours
			self.break_hours = 0.0

	def validate_duplicate_workday(self):
		workday = frappe.db.exists("Workday", {
			'employee': self.employee,
			'log_date': self.log_date
		})
	
		if workday and self.is_new():
			frappe.throw(
			_("{0} already exists for employee: {1}, on the given date: {2}")
			.format(frappe.get_desk_link("Workday", workday),self.employee, formatdate(self.log_date))
			)
	

def get_month_map():
	return frappe._dict({
		"January": 1,
		"February": 2,
		"March": 3,
		"April": 4,
		"May": 5,
		"June": 6,
		"July": 7,
		"August": 8,
		"September": 9,
		"October": 10,
		"November": 11,
		"December": 12
		})
	
@frappe.whitelist()
def get_unmarked_days(employee, month, exclude_holidays=0):
	import calendar
	month_map = get_month_map()	
	today = get_datetime()	

	joining_date, relieving_date = frappe.get_cached_value("Employee", employee, ["date_of_joining", "relieving_date"])
	start_day = 1
	end_day = calendar.monthrange(today.year, month_map[month])[1] + 1

	if joining_date and joining_date.month == month_map[month]:
		start_day = joining_date.day

	if relieving_date and relieving_date.month == month_map[month]:
		end_day = relieving_date.day + 1

	dates_of_month = ['{}-{}-{}'.format(today.year, month_map[month], r) for r in range(start_day, end_day)]
	month_start, month_end = dates_of_month[0], dates_of_month[-1]

	marked_days = [] 
	if cint(exclude_holidays):
		holiday_dates = get_holiday_dates_for_employee(employee, month_start, month_end)
		holidays = [get_datetime(rcord) for rcord in holiday_dates]
		marked_days.extend(holidays)

	unmarked_days = []

	for date in dates_of_month:
		date_time = get_datetime(date)
		if today.day <= date_time.day and today.month <= date_time.month:
			break
		if date_time not in marked_days:
			unmarked_days.append(date)

	return unmarked_days


@frappe.whitelist()
def get_unmarked_range(employee, from_day, to_day):
	joining_date, relieving_date = frappe.get_cached_value("Employee", employee, ["date_of_joining", "relieving_date"])
	
	start_day = from_day
	end_day = to_day

	if joining_date and joining_date >= getdate(from_day):
		start_day = joining_date
	if relieving_date and relieving_date >= getdate(to_day):
		end_day = relieving_date

	delta = date_diff(end_day, start_day)	
	days_of_list = ['{}'.format(add_days(start_day,i)) for i in range(delta + 1)]	
	month_start, month_end = days_of_list[0], days_of_list[-1]	

	rcords = frappe.get_list("Workday", fields=['log_date','employee'], filters=[
		["log_date",">=",month_start],
		["log_date","<=",month_end],
		["employee","=",employee]
	])
	
	marked_days = [get_datetime(rcord.log_date) for rcord in rcords]
	unmarked_days = []

	for date in days_of_list:
		date_time = get_datetime(date)
		if date_time not in marked_days:
			unmarked_days.append(date)

	return unmarked_days


@frappe.whitelist()
def get_created_workdays(employee, date_from, date_to):
	workday_list = frappe.get_list(
		"Workday",
		filters={
			"employee": employee,
			"log_date": ["between", [date_from, date_to]],
		},
		fields=["log_date","name"],
		order_by="log_date asc" 
	)
	
	formatted_workdays = []
	for workday in workday_list:
		date_obj = getdate(workday['log_date'])
		formatted_date = formatdate(date_obj, 'dd.MM.yyyy')
		formatted_workdays.append({
			'log_date': formatted_date,
			'name':workday['name']
		})
	
	return formatted_workdays


def get_employee_checkin(employee,atime):
    EmployeeCheckin = frappe.qb.DocType('Employee Checkin')
    checkin_list = (
        frappe.qb.from_(EmployeeCheckin)
        .select(
            EmployeeCheckin.name,
            EmployeeCheckin.log_type,
            EmployeeCheckin.time,
            EmployeeCheckin.skip_auto_attendance,
            EmployeeCheckin.attendance
        )
        .where(EmployeeCheckin.employee == employee)
        .where(Date(EmployeeCheckin.time) == getdate(atime))
        .orderby(EmployeeCheckin.time, order=Order.asc)
    ).run(as_dict=1)

    return checkin_list or []


def get_employee_default_work_hour(employee, adate):
    adate = getdate(adate)
    dayname = adate.strftime('%A')

    WeeklyWorkingHours = DocType("Weekly Working Hours")
    DailyHoursDetail = DocType("Daily Hours Detail")

    query = (
        frappe.qb.from_(WeeklyWorkingHours)
        .left_join(DailyHoursDetail)
        .on(WeeklyWorkingHours.name == DailyHoursDetail.parent)
        .select(
            WeeklyWorkingHours.name,
            WeeklyWorkingHours.employee,
            WeeklyWorkingHours.valid_from,
            WeeklyWorkingHours.valid_to,
            WeeklyWorkingHours.no_break_hours,
            WeeklyWorkingHours.set_target_hours_to_zero_when_date_is_holiday,
            DailyHoursDetail.day,
            DailyHoursDetail.hours,
            DailyHoursDetail.break_minutes
        )
        .where(
            (WeeklyWorkingHours.employee == employee)
            & (DailyHoursDetail.day == dayname)
            & (WeeklyWorkingHours.valid_from <= adate)
            & (WeeklyWorkingHours.valid_to >= adate)
            & (WeeklyWorkingHours.docstatus == 1)
        )
    )

    target_work_hours = query.run(as_dict=True)

    if not target_work_hours:
        frappe.throw(_('Please create Weekly Working Hours for the selected Employee:{0} first for date : {1}.').format(employee,adate))

    if len(target_work_hours) > 1:
        target_work_hours= "<br> ".join([frappe.get_desk_link("Weekly Working Hours", w.name) for w in target_work_hours])
        frappe.throw(_('There exist multiple Weekly Working Hours exist for the Date <b>{0}</b>: <br>{1} <br>').format(adate, target_work_hours))

    return target_work_hours[0]


@frappe.whitelist()
def get_actual_employee_log(aemployee, adate):
    employee_checkins = get_employee_checkin(aemployee,adate)
    employee_default_work_hour = get_employee_default_work_hour(aemployee,adate)
    is_date_in_holiday_list = date_is_in_holiday_list(aemployee,adate)
    no_break_hours = employee_default_work_hour.no_break_hours
    is_target_hours_zero_on_holiday = employee_default_work_hour.set_target_hours_to_zero_when_date_is_holiday
    is_holiday_with_zero_target_hours = is_target_hours_zero_on_holiday and is_date_in_holiday_list

    if employee_checkins and not is_holiday_with_zero_target_hours:
        new_workday = get_workday(employee_checkins, employee_default_work_hour, no_break_hours)
        return new_workday
    else:
        view_employee_attendance = get_employee_attendance(aemployee, adate)
        
        break_minutes = employee_default_work_hour.break_minutes
        expected_break_hours = flt(break_minutes / 60)
        
        if is_holiday_with_zero_target_hours:
            new_workday = {
                "target_hours": 0,
                "break_minutes": employee_default_work_hour.break_minutes,
                "actual_working_hours": 0,
                "hours_worked": 0,
                "nbreak": 0,
                "attendance": view_employee_attendance[0].name if len(view_employee_attendance) > 0 else "",
				"status": view_employee_attendance[0].status if len(view_employee_attendance) > 0 else "",
                "break_hours": 0,
                "employee_checkins": [],
                "first_checkin": "",
                "last_checkout": "",
                "expected_break_hours": 0,
            }
        else:
            new_workday = {
                "target_hours": employee_default_work_hour.hours,
                "break_minutes": employee_default_work_hour.break_minutes,
                "actual_working_hours": -employee_default_work_hour.hours,
                "manual_workday": 1,
                "hours_worked": 0,
                "nbreak": 0,
                "attendance": view_employee_attendance[0].name if len(view_employee_attendance) > 0 else "",
				"status": view_employee_attendance[0].status if len(view_employee_attendance) > 0 else "",
                "break_hours": 0,
                "employee_checkins": [],
                "first_checkin": "",
                "last_checkout": "",
                "expected_break_hours": expected_break_hours,
            }

    return new_workday


@frappe.whitelist()
def set_attendance_in_employee_checkins(employee_checkins, attendance):
	if isinstance(employee_checkins, str):
		import json
		employee_checkins = json.loads(employee_checkins)
	if not employee_checkins:
		return

	checkin_updated = False
	for checkin in employee_checkins:
		checkin
		if checkin.get("employee_checkin") is None:
			continue
		checkin_doc = frappe.get_doc("Employee Checkin", checkin.get("employee_checkin"))
		if checkin_doc.attendance == attendance:
			continue
		checkin_doc.attendance = attendance
		checkin_doc.save()
		checkin_updated = True

	return checkin_updated

def get_workday(employee_checkins, employee_default_work_hour, no_break_hours):
    hr_addon_settings = frappe.get_cached_doc("HR Addon Settings")
    is_break_from_checkins_with_swapped_hours = hr_addon_settings.workday_break_calculation_mechanism == "Break Hours from Employee Checkins" and hr_addon_settings.swap_hours_worked_and_actual_working_hours
    new_workday = {}

    hours_worked = 0.0
    total_duration = 0
    first_checkin = ""
    last_checkout = ""

    # not pair of IN/OUT either missing
    if len(employee_checkins)% 2 != 0:
        hours_worked = -36.0
        employee_checkin_message = ""
        for d in employee_checkins:
            employee_checkin_message += "<li>CheckIn Type:{0} for {1}</li>".format(d.log_type, frappe.get_desk_link("Employee Checkin", d.name))

        frappe.msgprint("CheckIns must be in pair for the given date:<ul>{}</ul>".format(employee_checkin_message))
        return new_workday

    if (len(employee_checkins) % 2 == 0):
        # seperate 'IN' from 'OUT'
        clockin_list = [get_datetime(kin.time) for x,kin in enumerate(employee_checkins) if x % 2 == 0]
        clockout_list = [get_datetime(kout.time) for x,kout in enumerate(employee_checkins) if x % 2 != 0]

        # get total worked hours
        for i in range(len(clockin_list)):
            wh = time_diff_in_hours(clockout_list[i],clockin_list[i])
            hours_worked += float(str(wh))

        # Calculate difference between first check-in and last checkout
        if clockin_list and clockout_list:
            first_checkin = clockin_list[0]
            last_checkout = clockout_list[-1]  # Last element of clockout_list
            total_duration = time_diff_in_hours(last_checkout, first_checkin) 

        if is_break_from_checkins_with_swapped_hours:
            total_duration, hours_worked = hours_worked, total_duration

    default_break_minutes = employee_default_work_hour.break_minutes
    default_break_hours = flt(default_break_minutes / 60)
    target_hours = employee_default_work_hour.hours

    if len(employee_checkins) % 2 == 0:
        break_from_checkins = 0.0
        for i in range(len(clockout_list) - 1):
            wh = time_diff_in_hours(clockin_list[i + 1], clockout_list[i])
            break_from_checkins += float(wh)

        if hr_addon_settings.workday_break_calculation_mechanism == "Break Hours from Employee Checkins":
            break_hours = break_from_checkins

        elif hr_addon_settings.workday_break_calculation_mechanism == "Break Hours from Weekly Working Hours":
            break_hours = default_break_hours

        elif hr_addon_settings.workday_break_calculation_mechanism == "Break Hours from Weekly Working Hours if Shorter breaks":
            if break_from_checkins <= default_break_hours:
                break_hours = default_break_hours
            else:
                break_hours = break_from_checkins
        else:
            break_hours = 0.0

    else:
        break_hours = flt(-360.0)

    hours_worked = flt(hours_worked)

    if is_break_from_checkins_with_swapped_hours:
        #swapping for gall
        if hours_worked > 0:
            actual_working_hours = hours_worked - break_hours
        else:
            actual_working_hours = total_duration - default_break_hours

    else:
        if total_duration > 0:
            actual_working_hours = total_duration - break_hours
        else:    
            actual_working_hours = hours_worked - default_break_hours
    attendance = employee_checkins[0].attendance if len(employee_checkins) > 0 else ""
    status = frappe.db.get_value("Attendance", attendance, "status") if attendance else ""

    if no_break_hours and hours_worked < 6 and not is_break_from_checkins_with_swapped_hours: # TODO: set 6 as constant
        default_break_minutes = 0
        #expected_break_hours = 0
        actual_working_hours = hours_worked

    new_workday.update({
        "target_hours": target_hours,
        "break_minutes": default_break_minutes,
        "hours_worked": hours_worked,
        "expected_break_hours": default_break_hours,
        "actual_working_hours": actual_working_hours,
        "nbreak": 0,
        "attendance": attendance,
        "status": status,
        "break_hours": break_hours,
        "first_checkin": first_checkin,
        "last_checkout": last_checkout,
        "employee_checkins":employee_checkins,
    })

    return new_workday


def get_employee_attendance(employee,atime):
    Attendance = frappe.qb.DocType('Attendance')
    attendance_list = (
        frappe.qb.from_(Attendance)
        .select(
            Attendance.name,
            Attendance.employee,
            Attendance.status,
            Attendance.attendance_date,
            Attendance.shift
        )
        .where(Attendance.employee == employee)
        .where(Date(Attendance.attendance_date) == getdate(atime))
        .where(Attendance.docstatus == 1)
        .orderby(Attendance.attendance_date, order=Order.asc)
    ).run(as_dict=True)

    return attendance_list


@frappe.whitelist()
def date_is_in_holiday_list(employee, date):
    holiday_list = frappe.get_cached_value("Employee", employee, "holiday_list")
    if not holiday_list:
        frappe.msgprint(_("Holiday list not set in {0}").format(employee))
        return False

    Holiday = frappe.qb.DocType('Holiday')
    holidays = (
        frappe.qb.from_(Holiday)
        .select(Holiday.holiday_date)
        .where(Holiday.parent == holiday_list)
        .where(Holiday.holiday_date == getdate(date))
    ).run()

    return len(holidays) > 0


def create_background_job_for_workday_generation(hr_addon_settings):
	from frappe.core.doctype.scheduled_job_type.scheduled_job_type import insert_single_event	
	if hr_addon_settings.enabled == 0:
		return

	time = hr_addon_settings.time
	background_job_frequency = hr_addon_settings.background_job_frequency
	
	name2number_dict = {
		"Sunday": 0,
		"Monday": 1,
		"Tuesday": 2,
		"Wednesday": 3,
		"Thursday": 4,
		"Friday": 5,
		"Saturday": 6
	}
	
	if background_job_frequency == "Daily":
		cron_string = "0 {0} * * *".format(time)
		frequency = "Cron"
	elif background_job_frequency == "Weekly":
		day_number = name2number_dict.get(hr_addon_settings.day)
		cron_string = "0 {0} * * {1}".format(time, day_number)
		frequency = "Cron"
	else:
		cron_string = "0 {0} * * *".format(time)
		frequency = "Cron"
	
	insert_single_event(
		frequency=frequency,
		event="hr_addon.hr_addon.doctype.workday.workday.generate_workdays_scheduled_job",
		cron_format=cron_string
	)


def create_background_job_for_workday_generation_after_migrate():
	hr_addon_settings = frappe.get_cached_doc("HR Addon Settings")
	create_background_job_for_workday_generation(hr_addon_settings)

def generate_workdays_scheduled_job():
	hr_addon_settings = frappe.get_doc("HR Addon Settings")
	if hr_addon_settings.enabled == 0:
		return
	generate_workdays_for_past_7_days_now()


@frappe.whitelist()
def generate_workdays_for_past_7_days_now():
	today = frappe.utils.datetime.datetime.now()
	a_week_ago = today - frappe.utils.datetime.timedelta(days=7)
	employees = frappe.db.get_list("Employee", filters={"status": "Active"})
	for employee in employees:
		try:
			employee_name = employee["name"]
			unmarked_days = get_unmarked_range(employee_name, a_week_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
			valid_unmarked_days = []
			for date in unmarked_days:
				if has_valid_weekly_working_hours(employee_name, date):
					valid_unmarked_days.append(date)
			
			if not valid_unmarked_days:
				continue  # No valid dates, skip

			data = {
				"employee": employee_name,
				"unmarked_days": valid_unmarked_days
			}
			flag = "Create workday"

			bulk_process_workdays_background(data, flag)
		except Exception as e:
			frappe.log_error(
				"Creating Workday, Got Error: {} while fetching unmarked days for: {}".format(str(e), employee_name),
				"Error during fetching unmarked days"
			)

def has_valid_weekly_working_hours(employee, date):
	date = frappe.utils.getdate(date)
	dayname = date.strftime('%A')

	weekly_hours = frappe.db.get_all(
		"Weekly Working Hours",
		filters={
			"employee": employee,
			"docstatus": 1,
			"valid_from": ["<=", date],
			"valid_to": [">=", date],
		},
		fields=["name"]
	)

	if not weekly_hours:
		return False

	parent_names = [wh.name for wh in weekly_hours]

	daily_hours = frappe.db.exists(
		"Daily Hours Detail",
		{
			"parent": ["in", parent_names],
			"day": dayname
		}
	)

	return True if daily_hours else False 			


def bulk_process_workdays_background(data,flag):
	'''bulk workday processing'''
	frappe.msgprint(_("Bulk operation is enqueued in background."), alert=True)
	frappe.enqueue(
		'hr_addon.hr_addon.doctype.workday.workday.bulk_process_workdays',
		queue='long',
		data=data,
		flag=flag
	)


@frappe.whitelist()
def bulk_process_workdays(data,flag):
	import json
	if isinstance(data, str):
		data = json.loads(data)
	data = frappe._dict(data)

	if data.employee and frappe.get_value('Employee', data.employee, 'status') != "Active":
		frappe.throw(_("{0} is not active").format(frappe.get_desk_link('Employee', data.employee)))

	company = frappe.get_value('Employee', data.employee, 'company')
	if not data.unmarked_days:
		frappe.throw(_("Please select a date"))
		return

	missing_dates = []

	for date in data.unmarked_days:
		try:
			if not frappe.db.exists('Workday', {'employee': data.employee,'log_date': get_datetime(date)}):
				workday = frappe.new_doc("Workday")
				workday.employee = data.employee
				workday.company = company
				workday.log_date = get_datetime(date)
				if flag == "Create workday":
					workday.save()

			missing_dates.append(get_datetime(date))

		except Exception:
			message = _("Something went wrong in Workday Creation: {0}".format(traceback.format_exc()))
			frappe.msgprint(message)
			frappe.log_error("bulk_process_workdays() error", message)

	formatted_missing_dates = []
	for missing_date in missing_dates:
		formatted_m_date = formatdate(missing_date,'dd.MM.yyyy')
		formatted_missing_dates.append(formatted_m_date)

	return {
		"message": 1,
		"missing_dates": formatted_missing_dates,
		"flag":flag
	}


@frappe.whitelist()
def create_workday_from_api(data):
	"""
	Create a Workday document with custom data from API (e.g., n8n workflow).
	This function bypasses the automatic data fetching and uses the provided data directly.
	
	Args:
		data (dict or str): Dictionary containing workday data or JSON string
		
	Expected data structure:
	{
		"employee": "HR-EMP-00001",
		"employee_name": "Max Mustermann",
		"attendance_date": "2025-11-10",
		"start_time": "06:30:00",
		"end_time": "16:30:00",
		"total_hours": 10,
		"break_hours": 0.75,
		"net_hours": 9.25,
		"expected_hours": 8,
		"expected_break_hours": 0.5,  # Optional, defaults to break_hours
		"status": "Present",
		"attendance": "ATT-00001",  # Optional, link to Attendance document
		"total_checkins": 2,
		"first_checkin": "10.11.2025, 06:30:00",
		"last_checkout": "10.11.2025, 16:30:00"
	}
	
	Returns:
		dict: Created workday document as dict
	"""
	import json
	from datetime import datetime
	
	# Parse JSON string if necessary
	if isinstance(data, str):
		data = json.loads(data)
	
	# If data is a list, take the first element
	if isinstance(data, list):
		if not data:
			frappe.throw(_("Empty data array provided"))
		data = data[0]
	
	# Validate required fields
	if not data.get("employee"):
		frappe.throw(_("Employee is required"))
	if not data.get("attendance_date"):
		frappe.throw(_("Attendance date is required"))
	
	# Parse dates and times
	log_date = getdate(data.get("attendance_date"))
	
	# Parse first_checkin and last_checkout if provided in German format
	first_checkin = None
	last_checkout = None
	
	# Debug: Log received data
	frappe.logger().info(f"API Data received: {json.dumps(data, indent=2)}")
	
	if data.get("first_checkin"):
		first_checkin_str = data.get("first_checkin")
		frappe.logger().info(f"Attempting to parse first_checkin: '{first_checkin_str}' (type: {type(first_checkin_str)})")
		try:
			# Try parsing German format: "10.11.2025, 06:30:00"
			first_checkin = datetime.strptime(first_checkin_str, "%d.%m.%Y, %H:%M:%S")
			frappe.logger().info(f"Successfully parsed first_checkin as German format: {first_checkin}")
		except Exception as e:
			frappe.logger().warning(f"Failed to parse first_checkin as German format: {str(e)}")
			try:
				# Try ISO format or other formats
				first_checkin = get_datetime(first_checkin_str)
				frappe.logger().info(f"Successfully parsed first_checkin with get_datetime: {first_checkin}")
			except Exception as e2:
				# Log the error but continue
				frappe.log_error(f"Could not parse first_checkin: '{first_checkin_str}'\nFirst error: {str(e)}\nSecond error: {str(e2)}", "Workday API - Parse Error")
				frappe.logger().error(f"Could not parse first_checkin at all: {str(e2)}")
				first_checkin = None
	
	if data.get("last_checkout"):
		last_checkout_str = data.get("last_checkout")
		frappe.logger().info(f"Attempting to parse last_checkout: '{last_checkout_str}' (type: {type(last_checkout_str)})")
		try:
			# Try parsing German format: "10.11.2025, 16:30:00"
			last_checkout = datetime.strptime(last_checkout_str, "%d.%m.%Y, %H:%M:%S")
			frappe.logger().info(f"Successfully parsed last_checkout as German format: {last_checkout}")
		except Exception as e:
			frappe.logger().warning(f"Failed to parse last_checkout as German format: {str(e)}")
			try:
				# Try ISO format or other formats
				last_checkout = get_datetime(last_checkout_str)
				frappe.logger().info(f"Successfully parsed last_checkout with get_datetime: {last_checkout}")
			except Exception as e2:
				# Log the error but continue
				frappe.log_error(f"Could not parse last_checkout: '{last_checkout_str}'\nFirst error: {str(e)}\nSecond error: {str(e2)}", "Workday API - Parse Error")
				frappe.logger().error(f"Could not parse last_checkout at all: {str(e2)}")
				last_checkout = None
	
	frappe.logger().info(f"Final parsed values - first_checkin: {first_checkin}, last_checkout: {last_checkout}")
	
	# Check if workday already exists
	existing_workday = frappe.db.exists("Workday", {
		"employee": data.get("employee"),
		"log_date": log_date
	})
	
	if existing_workday:
		frappe.throw(
			_("Workday already exists for employee {0} on {1}").format(
				data.get("employee"), 
				formatdate(log_date)
			)
		)
	
	# Get company from employee
	company = frappe.db.get_value("Employee", data.get("employee"), "company")
	
	# Create workday document
	workday = frappe.new_doc("Workday")
	workday.skip_auto_fetch = True  # Skip automatic data fetching
	
	# Set basic fields
	workday.employee = data.get("employee")
	workday.employee_name = data.get("employee_name")
	workday.log_date = log_date
	workday.status = data.get("status", "")
	workday.company = company
	
	# Set attendance if provided
	if data.get("attendance"):
		workday.attendance = data.get("attendance")
	
	# Set hours
	workday.target_hours = flt(data.get("expected_hours", 0))
	workday.hours_worked = flt(data.get("total_hours", 0))
	workday.break_hours = flt(data.get("break_hours", 0))
	workday.actual_working_hours = flt(data.get("net_hours", 0))
	workday.expected_break_hours = flt(data.get("expected_break_hours", data.get("break_hours", 0)))
	
	# Set checkin/checkout times
	if first_checkin:
		workday.first_checkin = first_checkin
	if last_checkout:
		workday.last_checkout = last_checkout
	
	# Save the document
	workday.insert()
	
	return workday.as_dict()
